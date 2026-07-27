"""Graph subscription lifecycle (plan §8).

Subscriptions expire, and **Graph decides when** — it may grant less than we
asked for, and the documented maximum has changed more than once. So nothing
here assumes a duration: the renewal point is always derived from the
`expirationDateTime` Graph actually returned. Hardcoding "three days" is how a
subscription lapses quietly and ingestion stops looking like anything at all.

allow-hardcode: the SQL and Graph payloads below are test fixtures.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.rls import tenant_session
from app.services.graph.client import GraphClient
from app.services.graph.subscriptions import (
    build_subscription_payload,
    create_subscription,
    delete_subscription,
    renew_subscription,
    renewal_threshold,
)


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


def _graph(handler) -> GraphClient:
    return GraphClient(token="t", transport=httpx.MockTransport(handler))


def _responds(payload, status=200):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        if request.content:
            captured["body"] = json.loads(request.content)
        return httpx.Response(status, json=payload)

    return handler, captured


async def _subscription_row(tenant_id):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(text("SELECT * FROM graph_subscriptions"))
        ).one_or_none()


# --- renewal timing ---------------------------------------------------------


def test_the_renewal_point_comes_from_the_granted_lifetime():
    created = datetime(2026, 7, 27, tzinfo=UTC)

    assert renewal_threshold(created, created + timedelta(days=7)) == (
        created + timedelta(days=3, hours=12)
    )


def test_a_shorter_grant_is_renewed_sooner():
    """Graph may grant far less than requested. Renewing on an assumed three
    days would then miss the window entirely and the subscription would lapse.
    """
    created = datetime(2026, 7, 27, tzinfo=UTC)

    assert renewal_threshold(created, created + timedelta(hours=2)) == (
        created + timedelta(hours=1)
    )


def test_an_already_expired_subscription_is_due_immediately():
    created = datetime(2026, 7, 27, tzinfo=UTC)

    threshold = renewal_threshold(created, created - timedelta(hours=1))

    assert threshold <= created


def test_a_stale_basis_errs_towards_renewing_early_never_late():
    """Callers should pass `last_renewed_at` once a subscription has been
    renewed, but passing the original `created_at` is merely wasteful.

    The threshold is the weighted midpoint of the interval, so an older basis
    pulls it *earlier*. That is worth pinning down: the failure mode is extra
    Graph calls, not a subscription that lapses while the sweep waits.
    """
    renewed = datetime(2026, 7, 27, tzinfo=UTC)
    expires = renewed + timedelta(days=7)

    from_renewal = renewal_threshold(renewed, expires)
    assert from_renewal == renewed + timedelta(days=3, hours=12)

    for staleness in (timedelta(days=26), timedelta(days=365)):
        from_creation = renewal_threshold(renewed - staleness, expires)
        assert from_creation < from_renewal, "an older basis renews sooner"
        assert from_creation < expires, "and always before the subscription dies"


# --- the payload ------------------------------------------------------------


def test_each_subscription_gets_its_own_secret():
    """A shared secret makes every tenant's notifications forgeable the moment
    it leaks anywhere. Per-subscription limits that to one mailbox."""
    states = {
        build_subscription_payload("u", "f")["clientState"] for _ in range(20)
    }

    assert len(states) == 20


def test_the_secret_is_long_enough_to_be_worth_comparing():
    state = build_subscription_payload("u", "f")["clientState"]

    assert len(state) >= 32


@pytest.mark.parametrize("bad", [0, -0.5, 1.5])
def test_a_renew_margin_outside_the_safe_range_is_rejected(bad):
    """The bound is what makes a stale basis merely wasteful.

    Above 1 the renewal point falls *after* expiry and every subscription
    lapses silently — so it is enforced at config load rather than trusted.
    """
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            GRAPH_SUBSCRIPTION_RENEW_MARGIN=bad,
            APP_SECRET_KEY="x",
            TOKEN_ENCRYPTION_KEY="y",
            FRONTEND_ORIGIN="http://localhost",
            DATABASE_URL=settings.DATABASE_URL,
        )


def test_the_payload_targets_the_configured_folder():
    payload = build_subscription_payload("ms-user", "jobs-folder")

    assert payload["resource"] == "/users/ms-user/mailFolders/jobs-folder/messages"
    assert payload["changeType"] == "created"


def test_the_payload_registers_both_notification_urls():
    """Without the lifecycle URL, a revoked grant surfaces only as
    notifications quietly stopping."""
    payload = build_subscription_payload("u", "f")

    assert payload["notificationUrl"] == settings.MS_WEBHOOK_NOTIFICATION_URL
    assert payload["lifecycleNotificationUrl"] == settings.MS_WEBHOOK_LIFECYCLE_URL


def test_folder_ids_are_encoded_into_the_resource():
    """Graph ids are opaque. A stray `/` would not misroute a request — the
    resource travels in a JSON body — it would silently subscribe to something
    other than the folder the user chose."""
    payload = build_subscription_payload("user@x.com", "AAkAL/g+w==")

    assert payload["resource"] == (
        "/users/user%40x.com/mailFolders/AAkAL%2Fg%2Bw%3D%3D/messages"
    )


def test_the_payload_never_asks_for_resource_data():
    """Notifications stay lean: the message is fetched over an authenticated
    channel rather than trusted from the webhook body (plan §7)."""
    assert build_subscription_payload("u", "f")["includeResourceData"] is False


# --- creation ---------------------------------------------------------------


async def test_creating_a_subscription_stores_what_graph_granted(mailbox):
    """Not what we asked for. Graph is free to shorten it, and storing the
    request would put the renewal sweep on a schedule that does not exist.
    """
    tenant_id, mailbox_id = mailbox
    granted = "2026-07-28T00:00:00Z"
    handler, captured = _responds({"id": "sub-new", "expirationDateTime": granted})

    subscription_id = await create_subscription(
        tenant_id, mailbox_id, "ms-user", "jobs-folder", _graph(handler)
    )

    assert subscription_id == "sub-new"
    row = await _subscription_row(tenant_id)
    assert row.subscription_id == "sub-new"
    assert row.expires_at == datetime(2026, 7, 28, tzinfo=UTC)
    assert row.status == "active"
    assert row.client_state == captured["body"]["clientState"], (
        "the stored secret must be the one Graph was told"
    )
    assert captured["method"] == "POST"


async def test_the_stored_secret_is_what_the_webhook_will_compare(mailbox):
    tenant_id, mailbox_id = mailbox
    handler, captured = _responds(
        {"id": "sub-1", "expirationDateTime": "2026-07-28T00:00:00Z"}
    )

    await create_subscription(
        tenant_id, mailbox_id, "ms-user", "jobs-folder", _graph(handler)
    )

    row = await _subscription_row(tenant_id)
    assert len(row.client_state) >= 32


async def test_a_subscription_we_could_not_record_is_undone(monkeypatch, mailbox):
    """If the insert fails after Graph accepted the subscription, Graph holds a
    live subscription whose secret nobody stored.

    Its notifications would be rejected as forged until it expired days later,
    and nothing in the logs would connect the two. So it is deleted before the
    error propagates.
    """
    tenant_id, mailbox_id = mailbox
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                200, json={"id": "sub-orphan", "expirationDateTime": "2026-07-28T00:00:00Z"}
            )
        return httpx.Response(204, json={})

    from app.services.graph import subscriptions as module

    class _UnavailableDatabase:
        """`tenant_session` is an async context manager, not a coroutine."""

        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(module, "tenant_session", lambda *a, **k: _UnavailableDatabase())

    with pytest.raises(RuntimeError):
        await create_subscription(
            tenant_id, mailbox_id, "ms-user", "jobs-folder", _graph(handler)
        )

    assert seen == ["POST", "DELETE"], "the orphaned subscription must be removed"


# --- renewal ----------------------------------------------------------------


async def test_renewing_records_the_new_expiry_and_the_time_it_happened(
    admin_session, mailbox
):
    tenant_id, mailbox_id = mailbox
    await admin_session.execute(
        text(
            "INSERT INTO graph_subscriptions"
            " (id, tenant_id, mailbox_id, subscription_id, resource, client_state,"
            "  expires_at)"
            " VALUES (:id, :tenant, :mailbox, 'sub-r', 'r', 'secret',"
            "         now() + interval '1 hour')"
        ),
        {"id": uuid.uuid4(), "tenant": tenant_id, "mailbox": mailbox_id},
    )
    await admin_session.commit()

    handler, captured = _responds(
        {"id": "sub-r", "expirationDateTime": "2026-08-03T00:00:00Z"}
    )

    await renew_subscription(tenant_id, "sub-r", _graph(handler))

    row = await _subscription_row(tenant_id)
    assert row.expires_at == datetime(2026, 8, 3, tzinfo=UTC)
    assert row.last_renewed_at is not None
    assert row.status == "active"
    assert captured["method"] == "PATCH"
    assert captured["path"].endswith("/subscriptions/sub-r")


async def test_renewal_does_not_rotate_the_secret(admin_session, mailbox):
    """The webhook compares against the stored value. Rotating it here without
    telling Graph would reject every notification until the next renewal."""
    tenant_id, mailbox_id = mailbox
    await admin_session.execute(
        text(
            "INSERT INTO graph_subscriptions"
            " (id, tenant_id, mailbox_id, subscription_id, resource, client_state,"
            "  expires_at)"
            " VALUES (:id, :tenant, :mailbox, 'sub-k', 'r', 'keep-me',"
            "         now() + interval '1 hour')"
        ),
        {"id": uuid.uuid4(), "tenant": tenant_id, "mailbox": mailbox_id},
    )
    await admin_session.commit()
    handler, _ = _responds({"id": "sub-k", "expirationDateTime": "2026-08-03T00:00:00Z"})

    await renew_subscription(tenant_id, "sub-k", _graph(handler))

    assert (await _subscription_row(tenant_id)).client_state == "keep-me"


# --- deletion ---------------------------------------------------------------


async def test_deleting_retires_the_row_so_it_stops_routing(admin_session, mailbox):
    """A retired subscription must stop resolving, or notifications for a dead
    subscription keep pointing at a live mailbox."""
    tenant_id, mailbox_id = mailbox
    await admin_session.execute(
        text(
            "INSERT INTO graph_subscriptions"
            " (id, tenant_id, mailbox_id, subscription_id, resource, client_state,"
            "  expires_at)"
            " VALUES (:id, :tenant, :mailbox, 'sub-d', 'r', 'secret',"
            "         now() + interval '1 hour')"
        ),
        {"id": uuid.uuid4(), "tenant": tenant_id, "mailbox": mailbox_id},
    )
    await admin_session.commit()
    handler, captured = _responds({}, status=204)

    await delete_subscription(tenant_id, "sub-d", _graph(handler))

    assert captured["method"] == "DELETE"
    assert (await _subscription_row(tenant_id)).status == "deleted"

    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        resolved = (
            await session.execute(
                text("SELECT * FROM resolve_subscription(:s)"), {"s": "sub-d"}
            )
        ).one_or_none()
    assert resolved is None


async def test_deleting_a_subscription_graph_already_dropped_still_retires_it(
    admin_session, mailbox
):
    """404 means the desired state already holds; the row must still be
    retired or the resolver keeps routing to a subscription that is gone."""
    tenant_id, mailbox_id = mailbox
    await admin_session.execute(
        text(
            "INSERT INTO graph_subscriptions"
            " (id, tenant_id, mailbox_id, subscription_id, resource, client_state,"
            "  expires_at)"
            " VALUES (:id, :tenant, :mailbox, 'sub-gone', 'r', 'secret',"
            "         now() + interval '1 hour')"
        ),
        {"id": uuid.uuid4(), "tenant": tenant_id, "mailbox": mailbox_id},
    )
    await admin_session.commit()
    handler, _ = _responds({}, status=404)

    await delete_subscription(tenant_id, "sub-gone", _graph(handler))

    assert (await _subscription_row(tenant_id)).status == "deleted"


# --- the constraint carried forward from the Task 5 review ------------------


async def test_an_empty_secret_cannot_be_stored(admin_session, mailbox):
    """The webhook already refuses an empty secret, and that is the enforcement
    point. But a row carrying one should be impossible to create, not merely
    harmless to receive.
    """
    tenant_id, mailbox_id = mailbox

    with pytest.raises(IntegrityError):
        await admin_session.execute(
            text(
                "INSERT INTO graph_subscriptions"
                " (id, tenant_id, mailbox_id, subscription_id, resource, client_state,"
                "  expires_at)"
                " VALUES (:id, :tenant, :mailbox, 'sub-empty', 'r', '',"
                "         now() + interval '1 hour')"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id, "mailbox": mailbox_id},
        )
    await admin_session.rollback()
