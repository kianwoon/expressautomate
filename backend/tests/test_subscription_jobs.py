"""The lifecycle and reconciliation jobs (plan §8, §9).

Every one of these is enqueued by code that already ships: the webhook's
lifecycle endpoint, and the `delta_sync_all` / `renew_subscriptions` sweeps.
Until they exist, those producers hand arq job names nothing implements — the
recovery layer is fully wired on one side and empty on the other, which looks
identical to a quiet week.

allow-hardcode: the SQL and Graph payloads below are test fixtures.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.graph.client import GraphClient
from app.workers import jobs


@pytest.fixture
async def mailbox(admin_session):
    tenant_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"a-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user', 'jobs-folder', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    await admin_session.commit()
    yield tenant_id, mailbox_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def _add_subscription(session, tenant_id, mailbox_id, subscription_id):
    await session.execute(
        text(
            "INSERT INTO graph_subscriptions"
            " (id, tenant_id, mailbox_id, subscription_id, resource, client_state,"
            "  expires_at)"
            " VALUES (:id, :tenant, :mailbox, :sub, 'r', 'secret',"
            "         now() + interval '1 hour')"
        ),
        {
            "id": uuid.uuid4(),
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "sub": subscription_id,
        },
    )


def _wire(monkeypatch, handler):
    queued: list[tuple[str, dict]] = []

    async def _client(tenant_id, mailbox_id):
        return GraphClient(token="t", transport=httpx.MockTransport(handler))

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _client)
    monkeypatch.setattr(jobs, "enqueue", _enqueue)
    return queued


def _creates(subscription_id="sub-new"):
    expiry = (datetime.now(UTC) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201, json={"id": subscription_id, "expirationDateTime": expiry}
            )
        return httpx.Response(204, json={})

    return handler


async def _subscriptions(tenant_id):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text(
                    "SELECT subscription_id, status FROM graph_subscriptions"
                    " ORDER BY created_at"
                )
            )
        ).all()


# --- recreate_subscription --------------------------------------------------


async def test_recreating_retires_the_old_subscription_first(
    monkeypatch, admin_session, mailbox
):
    """`subscription_id` is unique and the resolver only routes active rows.

    Leaving the old row active would both collide on insert and keep pointing
    notifications at a subscription that no longer exists.
    """
    tenant_id, mailbox_id = mailbox
    await _add_subscription(admin_session, tenant_id, mailbox_id, "sub-old")
    await admin_session.commit()
    _wire(monkeypatch, _creates())

    await jobs.recreate_subscription(
        {}, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )

    rows = {row.subscription_id: row.status for row in await _subscriptions(tenant_id)}
    assert rows == {"sub-old": "replaced", "sub-new": "active"}


async def test_recreating_reconciles_the_gap_it_leaves(
    monkeypatch, admin_session, mailbox
):
    """Notifications stopped while the subscription was dead, so whatever
    arrived in that window is only reachable through a delta walk."""
    tenant_id, mailbox_id = mailbox
    await _add_subscription(admin_session, tenant_id, mailbox_id, "sub-old")
    await admin_session.commit()
    queued = _wire(monkeypatch, _creates())

    await jobs.recreate_subscription(
        {}, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )

    assert [name for name, _ in queued] == ["delta_sync_mailbox"]
    assert queued[0][1]["tenant_id"] == str(tenant_id)
    assert queued[0][1]["mailbox_id"] == str(mailbox_id)


async def test_recreating_works_when_there_was_no_subscription(
    monkeypatch, admin_session, mailbox
):
    """`subscriptionRemoved` can arrive for one we already retired."""
    tenant_id, mailbox_id = mailbox
    _wire(monkeypatch, _creates())

    await jobs.recreate_subscription(
        {}, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )

    rows = await _subscriptions(tenant_id)
    assert [row.status for row in rows] == ["active"]


async def test_a_dead_grant_stops_recreation_and_flags_the_mailbox(
    monkeypatch, admin_session, mailbox
):
    """Recreating needs the same grant that just failed."""
    from app.services.ms_auth import MailboxNotAuthorised

    tenant_id, mailbox_id = mailbox

    async def _client(tenant, mbox):
        raise MailboxNotAuthorised("refresh token rejected")

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _client)

    await jobs.recreate_subscription(
        {}, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )

    async with tenant_session(tenant_id) as session:
        status = (
            await session.execute(
                text("SELECT status FROM mailboxes WHERE id = :id"), {"id": mailbox_id}
            )
        ).scalar_one()
    assert status == "needs_reauth"
    assert await _subscriptions(tenant_id) == []


async def test_a_transient_refresh_failure_defers_recreation(
    monkeypatch, admin_session, mailbox
):
    """Entra throttling must defer recreation, not flag the mailbox.

    Before the transient/permanent split a throttled token refresh raised
    `MailboxNotAuthorised`, so `recreate_subscription` (enqueued after a
    `subscriptionRemoved` lifecycle event, or by the renewal sweep) marked the
    mailbox `needs_reauth` and forced a manual reconnect for a grant that was
    perfectly healthy.
    """
    from arq import Retry

    from app.services.ms_auth import TokenRefreshTransientError

    tenant_id, mailbox_id = mailbox

    async def _throttled(tenant, mbox):
        raise TokenRefreshTransientError("AADSTS900429: temporarily unavailable")

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _throttled)

    with pytest.raises(Retry):
        await jobs.recreate_subscription(
            {}, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
        )

    async with tenant_session(tenant_id) as session:
        status = (
            await session.execute(
                text("SELECT status FROM mailboxes WHERE id = :id"), {"id": mailbox_id}
            )
        ).scalar_one()
    assert status == "active", "a transient blip must not force a reconnect"


# --- reauthorize_subscription -----------------------------------------------


async def test_a_healthy_grant_renews_in_place(monkeypatch, admin_session, mailbox):
    """Graph asked us to prove the grant still works. A successful renewal is
    the proof — there is no need to replace a working subscription."""
    tenant_id, mailbox_id = mailbox
    await _add_subscription(admin_session, tenant_id, mailbox_id, "sub-live")
    await admin_session.commit()

    expiry = (datetime.now(UTC) + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    _wire(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"id": "sub-live", "expirationDateTime": expiry}
        ),
    )

    await jobs.reauthorize_subscription(
        {},
        subscription_id="sub-live",
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )

    rows = await _subscriptions(tenant_id)
    assert [(r.subscription_id, r.status) for r in rows] == [("sub-live", "active")]


async def test_a_revoked_grant_flags_the_mailbox_rather_than_retrying(
    monkeypatch, admin_session, mailbox
):
    """The user has to reconnect. Retrying a revoked grant just buries that."""
    from app.services.ms_auth import MailboxNotAuthorised

    tenant_id, mailbox_id = mailbox
    await _add_subscription(admin_session, tenant_id, mailbox_id, "sub-dead")
    await admin_session.commit()

    async def _client(tenant, mbox):
        raise MailboxNotAuthorised("grant revoked")

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _client)

    await jobs.reauthorize_subscription(
        {},
        subscription_id="sub-dead",
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )

    async with tenant_session(tenant_id) as session:
        status = (
            await session.execute(
                text("SELECT status FROM mailboxes WHERE id = :id"), {"id": mailbox_id}
            )
        ).scalar_one()
    assert status == "needs_reauth"


# --- delta_sync_mailbox -----------------------------------------------------


async def test_the_sync_job_walks_the_mailbox(monkeypatch, admin_session, mailbox):
    tenant_id, mailbox_id = mailbox
    delta_link = "https://graph.microsoft.com/v1.0/delta?$deltatoken=t"
    queued = _wire(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"value": [{"id": "NEW-1"}], "@odata.deltaLink": delta_link}
        ),
    )

    from app.services.graph import delta as delta_module

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(delta_module, "enqueue", _enqueue)

    await jobs.delta_sync_mailbox(
        {}, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )

    async with tenant_session(tenant_id) as session:
        stored = (
            await session.execute(
                text("SELECT delta_link FROM mailboxes WHERE id = :id"),
                {"id": mailbox_id},
            )
        ).scalar_one()
    assert stored == delta_link
    assert [name for name, _ in queued] == ["fetch_email"]


async def test_the_sync_job_stops_quietly_when_the_grant_is_dead(
    monkeypatch, admin_session, mailbox
):
    """Every ten minutes on a dead mailbox, so it must not raise into the
    worker's error path each time."""
    from app.services.ms_auth import MailboxNotAuthorised

    tenant_id, mailbox_id = mailbox

    async def _client(tenant, mbox):
        raise MailboxNotAuthorised("grant revoked")

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _client)

    await jobs.delta_sync_mailbox(
        {}, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
    )

    async with tenant_session(tenant_id) as session:
        status = (
            await session.execute(
                text("SELECT status FROM mailboxes WHERE id = :id"), {"id": mailbox_id}
            )
        ).scalar_one()
    assert status == "needs_reauth"


async def test_the_sync_job_defers_when_entra_throttles(
    monkeypatch, admin_session, mailbox
):
    """A throttled refresh must defer, not flag the mailbox for reconnect.

    The delta sweep is the highest-frequency refresh in the system (every ten
    minutes per active mailbox), which made it the most common source of the
    false disconnect: any transient token-endpoint blip used to raise
    `MailboxNotAuthorised` and permanently flip the mailbox `needs_reauth`.
    """
    from arq import Retry

    from app.services.ms_auth import TokenRefreshTransientError

    tenant_id, mailbox_id = mailbox

    async def _throttled(tenant, mbox):
        raise TokenRefreshTransientError("AADSTS900429: temporarily unavailable")

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _throttled)

    with pytest.raises(Retry):
        await jobs.delta_sync_mailbox(
            {}, tenant_id=str(tenant_id), mailbox_id=str(mailbox_id)
        )

    async with tenant_session(tenant_id) as session:
        status = (
            await session.execute(
                text("SELECT status FROM mailboxes WHERE id = :id"), {"id": mailbox_id}
            )
        ).scalar_one()
    assert status == "active", "a transient blip must not force a reconnect"


# --- the registry ------------------------------------------------------------


def _registered_names(worker_settings) -> set[str]:
    """The names arq will actually answer to.

    An entry is either a bare coroutine, registered under its own name, or an
    `arq.worker.Function` wrapping one to carry a timeout — `parse_candidate_cv`
    is the latter. Reading `__name__` alone raises on the wrapper, and a guard
    that crashes on a correctly registered job is worse than no guard.
    """
    return {
        getattr(fn, "name", None) or fn.__name__ for fn in worker_settings.functions
    }


def test_every_job_the_lifecycle_endpoint_can_enqueue_is_registered():
    """A name with no function is an error inside arq, on the far side of the
    queue — the producer sees success and nothing surfaces.

    The expected names are read from the webhook's own mapping rather than
    listed here, so adding a lifecycle event without its job fails this test
    instead of failing silently in production.
    """
    from app.api.graph_webhook import _LIFECYCLE_JOBS
    from app.workers.settings import WorkerSettings

    registered = _registered_names(WorkerSettings)
    required = set(_LIFECYCLE_JOBS.values())

    assert required <= registered, f"unregistered: {sorted(required - registered)}"


def test_the_recovery_sweep_only_resumes_jobs_that_exist_or_are_planned():
    """`rescan_stuck` maps statuses to jobs. Any name there that is neither
    registered nor a known future job would be a silent dead end.

    `classify_email` and `extract_email` are the extraction plan's; naming them
    explicitly means this test starts failing the moment that plan lands
    without registering them.
    """
    from app.workers.settings import WorkerSettings
    from app.workers.tasks import RESUME_JOB

    registered = _registered_names(WorkerSettings)
    awaiting_extraction = {"classify_email", "extract_email"}

    unaccounted = set(RESUME_JOB.values()) - registered - awaiting_extraction
    assert unaccounted == set(), f"jobs with no implementation: {sorted(unaccounted)}"
