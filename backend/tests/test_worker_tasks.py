"""Periodic recovery tasks (plan §8, §9).

These run in the supervisor process, not in arq. The split is deliberate: arq
processes work, this process makes sure work exists to be processed. One can
fail without silencing the other.

`rescan_stuck` is the outbox net. Redis cannot join the Postgres transaction
that committed a row, so an enqueue that fails after commit leaves durable work
with no job attached. Without this sweep, success criterion 3 — "killing any
worker mid-flight loses no email" — is simply false.

These sweeps run across every tenant at once, so they read through narrow
`SECURITY DEFINER` functions rather than a role that bypasses RLS.

allow-hardcode: the SQL below is test fixture data.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.graph.client import GraphClient
from app.workers import tasks

TERMINAL = ("extracted", "no_opportunity", "skipped", "unfetchable", "failed")


@pytest.fixture
async def tenant(admin_session):
    tenant_id = uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"a-{tenant_id.hex[:8]}"},
    )
    await admin_session.commit()
    yield tenant_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def _add_mailbox(session, tenant_id, *, status="active", ms_user_id=None):
    mailbox_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, status, retention_months)"
            " VALUES (:id, :tenant, :user, 'inbox', 'folder', :status, 24)"
        ),
        {
            "id": mailbox_id,
            "tenant": tenant_id,
            "user": ms_user_id or f"ms-{mailbox_id.hex[:8]}",
            "status": status,
        },
    )
    return mailbox_id


async def _add_email(session, tenant_id, mailbox_id, status, *, age_minutes):
    row_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO email_messages"
            " (id, tenant_id, mailbox_id, graph_message_id, processing_status,"
            "  updated_at)"
            " VALUES (:id, :tenant, :mailbox, :graph_id, :status,"
            "         now() - make_interval(mins => :age))"
        ),
        {
            "id": row_id,
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "graph_id": f"G-{row_id.hex[:8]}",
            "status": status,
            "age": age_minutes,
        },
    )
    return row_id


@pytest.fixture
def queued(monkeypatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        calls.append((name, kwargs))
        return True

    monkeypatch.setattr(tasks, "enqueue", _enqueue)
    return calls


# --- rescan_stuck -----------------------------------------------------------


@pytest.mark.parametrize(
    "status,job",
    [
        ("pending", "fetch_email"),
        ("fetched", "classify_email"),
        ("classifying", "classify_email"),
        ("extracting", "extract_email"),
    ],
)
async def test_a_stalled_row_is_requeued_with_the_job_that_resumes_it(
    admin_session, tenant, queued, status, job
):
    """Criterion 3: killing a worker at ANY non-terminal status loses nothing."""
    mailbox_id = await _add_mailbox(admin_session, tenant)
    row_id = await _add_email(admin_session, tenant, mailbox_id, status, age_minutes=60)
    await admin_session.commit()

    requeued = await tasks.rescan_stuck()

    assert requeued == 1
    name, kwargs = queued[0]
    assert name == job
    assert kwargs["email_message_id"] == str(row_id)
    # Without the tenant the job raises TypeError inside the worker — on the
    # far side of the queue, where nothing is watching.
    assert kwargs["tenant_id"] == str(tenant)
    assert kwargs["mailbox_id"] == str(mailbox_id)


@pytest.mark.parametrize("status", TERMINAL)
async def test_a_finished_row_is_never_requeued(admin_session, tenant, queued, status):
    """`skipped` and `unfetchable` are outcomes, not interruptions. Requeueing
    them would loop forever on work that is already done."""
    mailbox_id = await _add_mailbox(admin_session, tenant)
    await _add_email(admin_session, tenant, mailbox_id, status, age_minutes=600)
    await admin_session.commit()

    assert await tasks.rescan_stuck() == 0
    assert queued == []


async def test_a_row_a_worker_is_still_holding_is_left_alone(
    admin_session, tenant, queued
):
    """Requeueing in-flight work would duplicate it, not recover it."""
    mailbox_id = await _add_mailbox(admin_session, tenant)
    await _add_email(admin_session, tenant, mailbox_id, "pending", age_minutes=1)
    await admin_session.commit()

    assert await tasks.rescan_stuck() == 0


async def test_working_statuses_get_a_longer_grace_period(
    admin_session, tenant, queued
):
    """A fetch or an extraction legitimately takes longer than a queue hop, so
    they are not swept on the same clock as `pending`."""
    mailbox_id = await _add_mailbox(admin_session, tenant)
    between = (settings.RESCAN_PENDING_MINUTES + settings.RESCAN_WORKING_MINUTES) // 2
    await _add_email(admin_session, tenant, mailbox_id, "extracting", age_minutes=between)
    await admin_session.commit()

    assert await tasks.rescan_stuck() == 0, "still inside the working grace period"


async def test_the_sweep_covers_every_tenant(admin_session, queued):
    """It runs unscoped by design — a tenant whose rows it could not see would
    have no recovery net at all."""
    tenants = []
    for _ in range(2):
        tenant_id = uuid.uuid4()
        await admin_session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
            {"id": tenant_id, "slug": f"a-{tenant_id.hex[:8]}"},
        )
        mailbox_id = await _add_mailbox(admin_session, tenant_id)
        await _add_email(admin_session, tenant_id, mailbox_id, "pending", age_minutes=60)
        tenants.append(tenant_id)
    await admin_session.commit()

    try:
        assert await tasks.rescan_stuck() == 2
        assert {kwargs["tenant_id"] for _, kwargs in queued} == {
            str(t) for t in tenants
        }
    finally:
        for tenant_id in tenants:
            await admin_session.execute(
                text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
            )
        await admin_session.commit()


# --- renew_subscriptions ----------------------------------------------------


async def _add_subscription(
    session, tenant_id, mailbox_id, *, expires_in, created_ago, renewed_ago=None
):
    subscription_id = f"sub-{uuid.uuid4().hex[:8]}"
    await session.execute(
        text(
            "INSERT INTO graph_subscriptions"
            " (id, tenant_id, mailbox_id, subscription_id, resource, client_state,"
            "  expires_at, created_at, last_renewed_at)"
            " VALUES (:id, :tenant, :mailbox, :sub, 'r', 'secret',"
            "         now() + make_interval(mins => :expires),"
            "         now() - make_interval(mins => :created),"
            # Cast required: asyncpg cannot infer the type of a NULL bind
            # inside CASE, and fails the whole statement with
            # AmbiguousParameterError.
            "         CASE WHEN cast(:renewed as int) IS NULL THEN NULL"
            "              ELSE now() - make_interval(mins => cast(:renewed as int))"
            "         END)"
        ),
        {
            "id": uuid.uuid4(),
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "sub": subscription_id,
            "expires": expires_in,
            "created": created_ago,
            "renewed": renewed_ago,
        },
    )
    return subscription_id


def _patch_graph(monkeypatch, handler):
    async def _client(tenant_id, mailbox_id):
        return GraphClient(token="t", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(tasks, "graph_client_for_mailbox", _client)


def _renews():
    expiry = (datetime.now(UTC) + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    return lambda request: httpx.Response(
        200, json={"id": "sub", "expirationDateTime": expiry}
    )


async def test_a_subscription_past_its_halfway_point_is_renewed(
    monkeypatch, admin_session, tenant, queued
):
    mailbox_id = await _add_mailbox(admin_session, tenant)
    # Created 90 minutes ago, expires in 10: well past the halfway point.
    await _add_subscription(
        admin_session, tenant, mailbox_id, expires_in=10, created_ago=90
    )
    await admin_session.commit()
    _patch_graph(monkeypatch, _renews())

    assert await tasks.renew_subscriptions() == 1


async def test_a_fresh_subscription_is_left_alone(
    monkeypatch, admin_session, tenant, queued
):
    mailbox_id = await _add_mailbox(admin_session, tenant)
    await _add_subscription(
        admin_session, tenant, mailbox_id, expires_in=1000, created_ago=1
    )
    await admin_session.commit()

    def _explode(request):
        raise AssertionError("Graph must not be called for a fresh subscription")

    _patch_graph(monkeypatch, _explode)

    assert await tasks.renew_subscriptions() == 0


async def test_renewal_is_measured_from_the_last_renewal_not_the_creation(
    monkeypatch, admin_session, tenant, queued
):
    """A long-lived subscription renewed a moment ago is not due again.

    Measuring from `created_at` forever would renew far more often than needed
    — harmless but wasteful, and the whole point of storing `last_renewed_at`.
    """
    mailbox_id = await _add_mailbox(admin_session, tenant)
    await _add_subscription(
        admin_session,
        tenant,
        mailbox_id,
        expires_in=1000,
        created_ago=100_000,
        renewed_ago=1,
    )
    await admin_session.commit()

    def _explode(request):
        raise AssertionError("a just-renewed subscription must not renew again")

    _patch_graph(monkeypatch, _explode)

    assert await tasks.renew_subscriptions() == 0


async def test_a_retired_subscription_is_never_renewed(
    monkeypatch, admin_session, tenant, queued
):
    mailbox_id = await _add_mailbox(admin_session, tenant)
    subscription_id = await _add_subscription(
        admin_session, tenant, mailbox_id, expires_in=10, created_ago=90
    )
    await admin_session.execute(
        text("UPDATE graph_subscriptions SET status = 'deleted' WHERE subscription_id = :s"),
        {"s": subscription_id},
    )
    await admin_session.commit()

    def _explode(request):
        raise AssertionError("Graph must not be called for a retired subscription")

    _patch_graph(monkeypatch, _explode)

    assert await tasks.renew_subscriptions() == 0


async def test_a_failed_renewal_queues_a_recreate(
    monkeypatch, admin_session, tenant, queued
):
    """A subscription that cannot be renewed has to be replaced, or the mailbox
    silently stops receiving notifications."""
    mailbox_id = await _add_mailbox(admin_session, tenant)
    await _add_subscription(
        admin_session, tenant, mailbox_id, expires_in=10, created_ago=90
    )
    await admin_session.commit()
    _patch_graph(monkeypatch, lambda request: httpx.Response(404, json={}))

    renewed = await tasks.renew_subscriptions()

    assert renewed == 0
    assert [name for name, _ in queued] == ["recreate_subscription"]
    assert queued[0][1]["tenant_id"] == str(tenant)
    assert queued[0][1]["mailbox_id"] == str(mailbox_id)


@pytest.mark.parametrize("status", ["needs_reauth", "disconnected"])
async def test_a_subscription_on_an_unusable_mailbox_is_not_swept(
    monkeypatch, admin_session, tenant, queued, status
):
    """A grant that cannot mint a token cannot renew a subscription either.

    Without this the sweep would raise, log a warning, and re-flip an
    already-flipped status every fifteen minutes until the subscription
    expired — noise that hides the failures worth reading.
    """
    mailbox_id = await _add_mailbox(admin_session, tenant, status=status)
    await _add_subscription(
        admin_session, tenant, mailbox_id, expires_in=10, created_ago=90
    )
    await admin_session.commit()

    def _explode(request):
        raise AssertionError("an unusable mailbox must not be contacted")

    _patch_graph(monkeypatch, _explode)

    assert await tasks.renew_subscriptions() == 0


async def test_a_dead_grant_does_not_abort_the_rest_of_the_sweep(
    monkeypatch, admin_session, tenant, queued
):
    """One unauthorised mailbox must not starve every subscription behind it.

    `subscriptions_due_for_renewal` has no mailbox-status filter, so a
    permanently dead grant would otherwise abort at the same row every fifteen
    minutes and the subscriptions after it would lapse — silent mail loss.
    """
    from app.services.ms_auth import MailboxNotAuthorised

    dead_mailbox = await _add_mailbox(admin_session, tenant, ms_user_id="dead")
    live_mailbox = await _add_mailbox(admin_session, tenant, ms_user_id="live")
    await _add_subscription(
        admin_session, tenant, dead_mailbox, expires_in=10, created_ago=90
    )
    await _add_subscription(
        admin_session, tenant, live_mailbox, expires_in=10, created_ago=90
    )
    await admin_session.commit()

    handler = _renews()

    async def _client(tenant_id, mailbox_id):
        if mailbox_id == dead_mailbox:
            raise MailboxNotAuthorised("refresh token rejected")
        return GraphClient(token="t", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(tasks, "graph_client_for_mailbox", _client)

    renewed = await tasks.renew_subscriptions()

    assert renewed == 1, "the healthy subscription is still renewed"

    status = (
        await admin_session.execute(
            text("SELECT status FROM mailboxes WHERE id = :id"), {"id": dead_mailbox}
        )
    ).scalar_one()
    assert status == "needs_reauth"
    assert queued == [], "recreating needs the same dead grant, so it is not queued"


# --- ensure_subscriptions ---------------------------------------------------
#
# The backstop for "active mailbox, no subscription". Nothing else detects it:
# `renew_subscriptions` only scans rows that exist, and `delta_sync_all`
# filters on mailbox status. A mailbox in that state looks healthy and receives
# nothing — §8's "lapsed subscription reads as a quiet week", reached by a
# different route.


async def test_an_active_mailbox_with_no_subscription_gets_one(
    admin_session, tenant, queued
):
    mailbox_id = await _add_mailbox(admin_session, tenant)
    await admin_session.commit()

    assert await tasks.ensure_subscriptions() == 1
    assert queued == [
        (
            "recreate_subscription",
            {"tenant_id": str(tenant), "mailbox_id": str(mailbox_id)},
        )
    ]


async def test_a_mailbox_that_already_has_one_is_left_alone(
    admin_session, tenant, queued
):
    mailbox_id = await _add_mailbox(admin_session, tenant)
    await _add_subscription(
        admin_session, tenant, mailbox_id, expires_in=1000, created_ago=1
    )
    await admin_session.commit()

    assert await tasks.ensure_subscriptions() == 0
    assert queued == []


async def test_a_retired_subscription_does_not_count(admin_session, tenant, queued):
    """The row surviving as `replaced` or `deleted` is exactly the orphan case
    `recreate_subscription` leaves behind when its create fails."""
    mailbox_id = await _add_mailbox(admin_session, tenant)
    subscription_id = await _add_subscription(
        admin_session, tenant, mailbox_id, expires_in=1000, created_ago=1
    )
    await admin_session.execute(
        text(
            "UPDATE graph_subscriptions SET status = 'replaced'"
            " WHERE subscription_id = :s"
        ),
        {"s": subscription_id},
    )
    await admin_session.commit()

    assert await tasks.ensure_subscriptions() == 1


@pytest.mark.parametrize("status", ["needs_reauth", "disconnected"])
async def test_a_mailbox_we_cannot_read_is_not_given_a_subscription(
    admin_session, tenant, queued, status
):
    """Creating one needs a working grant, so this would fail every time."""
    await _add_mailbox(admin_session, tenant, status=status)
    await admin_session.commit()

    assert await tasks.ensure_subscriptions() == 0


async def test_a_reconnected_mailbox_is_resubscribed_without_manual_help(
    admin_session, tenant, queued
):
    """The gap recorded during the Task 9 review.

    A mailbox flagged `needs_reauth` keeps its retired subscription. When the
    user reconnects and the mailbox goes active again, nothing previously
    recreated the subscription — it looked healthy and ingested nothing.
    """
    mailbox_id = await _add_mailbox(admin_session, tenant, status="needs_reauth")
    subscription_id = await _add_subscription(
        admin_session, tenant, mailbox_id, expires_in=1000, created_ago=1
    )
    await admin_session.execute(
        text(
            "UPDATE graph_subscriptions SET status = 'replaced'"
            " WHERE subscription_id = :s"
        ),
        {"s": subscription_id},
    )
    await admin_session.commit()

    assert await tasks.ensure_subscriptions() == 0, "not while it needs reauth"

    # The user reconnects.
    await admin_session.execute(
        text("UPDATE mailboxes SET status = 'active' WHERE id = :id"),
        {"id": mailbox_id},
    )
    await admin_session.commit()

    assert await tasks.ensure_subscriptions() == 1
    assert queued[0][1]["mailbox_id"] == str(mailbox_id)


# --- delta_sync_all ---------------------------------------------------------


async def test_every_active_mailbox_is_reconciled(admin_session, tenant, queued):
    mailbox_ids = {
        str(await _add_mailbox(admin_session, tenant)),
        str(await _add_mailbox(admin_session, tenant)),
    }
    await admin_session.commit()

    assert await tasks.delta_sync_all() == 2
    assert {name for name, _ in queued} == {"delta_sync_mailbox"}
    assert {kwargs["mailbox_id"] for _, kwargs in queued} == mailbox_ids


@pytest.mark.parametrize("status", ["needs_reauth", "disconnected"])
async def test_a_mailbox_we_cannot_read_is_not_swept(
    admin_session, tenant, queued, status
):
    """Sweeping a mailbox whose grant is dead just burns a token refresh and
    logs the same failure every ten minutes."""
    await _add_mailbox(admin_session, tenant, status=status)
    await admin_session.commit()

    assert await tasks.delta_sync_all() == 0
    assert queued == []
