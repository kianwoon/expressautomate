"""The settings surface. Authentication and tenant scope are the point.

allow-hardcode: this is a test module; literals below are fixture values.

ASGI transport, not TestClient: TestClient drives its own event loop and the
engine in app.db.session is pinned to the session-scoped one (see
test_glossary_api.py, which hit this first).
"""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP_LINKED, address_digest
from app.models.wa_session import (
    STATUS_CONNECTED,
    STATUS_DISCONNECTED,
    STATUS_LOGGED_OUT,
    STATUS_PAIRING,
    STATUS_RECONNECTING,
)
from app.services.notify.events import ALL_EVENT_KINDS, EVENT_OPPORTUNITY_NEW
from app.workers import queue


class _FakeRedis:
    """A minimal stand-in for the arq/redis pool's `incr`/`expire`.

    Nothing here opens a socket — this is the "in-memory Redis" the task
    calls for, following the same fake-the-pool convention as
    `tests/test_queue.py` rather than a real server.
    """

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_queue_pool():
    """The pool arq/queue caches is process-global; each test starts clean so
    a fake installed by one test can't leak into the next."""
    queue.reset_pool()
    yield
    queue.reset_pool()


@pytest.fixture
async def client() -> httpx.AsyncClient:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


@pytest.fixture
async def signed_in(admin_session, client):
    """A tenant, a user, and the session cookie that authenticates them."""
    from app.api.auth import SESSION_COOKIE, _session_serializer

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
        {"id": tenant_id, "slug": f"agency-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'r@a.sg', 'recruiter')"
        ),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.commit()
    cookie = _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)})
    client.cookies.set(SESSION_COOKIE, cookie)
    yield tenant_id, user_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def test_settings_requires_a_session(client) -> None:
    client.cookies.clear()
    response = await client.get("/api/notifications/settings")
    assert response.status_code == 401


async def test_settings_lists_every_event_kind(client, signed_in) -> None:
    """The screen cannot offer an event the backend does not know about."""
    response = await client.get("/api/notifications/settings")
    assert {e["kind"] for e in response.json()["events"]} == set(ALL_EVENT_KINDS)


async def test_settings_starts_with_no_destinations(client, signed_in) -> None:
    response = await client.get("/api/notifications/settings")
    assert response.json()["destinations"] == []


async def test_telegram_link_returns_a_deep_link(client, signed_in, monkeypatch) -> None:
    """Requires telegram_configured() to be True. Pinned explicitly rather
    than inherited from whatever the ambient environment supplies — CI's
    workflow env and a locally-sourced .env.test happened to agree, until CI's
    inline env: block was added without these keys and the endpoint started
    correctly returning 503 where this test still expected 200.
    """
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setattr(settings, "TELEGRAM_API_BASE_URL", "https://api.telegram.org")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "expressautomate_test_bot")

    response = await client.post("/api/notifications/destinations/telegram/link")
    assert response.status_code == 200
    assert response.json()["url"].startswith("https://t.me/")


async def test_telegram_link_is_503_when_telegram_is_not_configured(
    client, signed_in, monkeypatch
) -> None:
    """The deliberate counterpart to the test above: pins the unconfigured
    behaviour so it is exercised on purpose, not only by accident whenever an
    environment happens to be missing these keys (as CI's was).
    """
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_API_BASE_URL", "")

    response = await client.post("/api/notifications/destinations/telegram/link")
    assert response.status_code == 503


async def test_subscriptions_reject_an_unknown_event(
    client, signed_in, admin_session
) -> None:
    """A typo would otherwise be stored as a category nobody is subscribed to."""
    tenant_id, user_id = signed_in
    dest_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'x', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.commit()

    response = await client.put(
        "/api/notifications/subscriptions",
        json={"destination_id": str(dest_id), "event_kinds": ["opportunity.invented"]},
    )
    assert response.status_code == 422


async def test_subscriptions_cannot_target_another_tenants_destination(
    client, signed_in, admin_session
) -> None:
    """The most important test in this file."""
    other_tenant, other_dest = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'other', :slug)"),
        {"id": other_tenant, "slug": f"other-{other_tenant.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :ch, 'x', :hash, now())"
        ),
        {
            "id": other_dest,
            "tid": other_tenant,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("99999"),
        },
    )
    await admin_session.commit()
    try:
        response = await client.put(
            "/api/notifications/subscriptions",
            json={
                "destination_id": str(other_dest),
                "event_kinds": [EVENT_OPPORTUNITY_NEW],
            },
        )
        assert response.status_code == 404
    finally:
        await admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": other_tenant}
        )
        await admin_session.commit()


async def test_unticking_a_subscription_removes_it(
    client, signed_in, admin_session
) -> None:
    """The endpoint replaces rather than merges, precisely so that sending a
    smaller set is how the settings screen unticks a box. Nothing before this
    test exercised the removal half of that contract — every existing PUT
    only ever added kinds to a destination with none yet.
    """
    tenant_id, user_id = signed_in
    dest_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'x', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.commit()

    other_kind = next(k for k in ALL_EVENT_KINDS if k != EVENT_OPPORTUNITY_NEW)
    await client.put(
        "/api/notifications/subscriptions",
        json={
            "destination_id": str(dest_id),
            "event_kinds": [EVENT_OPPORTUNITY_NEW, other_kind],
        },
    )

    response = await client.put(
        "/api/notifications/subscriptions",
        json={"destination_id": str(dest_id), "event_kinds": [EVENT_OPPORTUNITY_NEW]},
    )
    assert response.status_code == 200

    settings_response = await client.get("/api/notifications/settings")
    destination = settings_response.json()["destinations"][0]
    assert destination["event_kinds"] == [EVENT_OPPORTUNITY_NEW]


async def test_opt_in_is_rate_limited(client, signed_in, monkeypatch) -> None:
    """Otherwise this endpoint is an OTP pump on our WABA's reputation.

    Needs whatsapp_configured() to be True, or the endpoint 503s before the
    rate limit is ever reached — pin the settings it checks explicitly rather
    than relying on the ambient environment to supply them.
    """
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "test-wa-token")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "100000000000000")
    monkeypatch.setattr(
        settings, "WHATSAPP_API_BASE_URL", "https://graph.facebook.com/v21.0"
    )
    sent: list[str] = []

    class FakeChannel:
        async def send(self, address, content):
            from app.services.notify.channels.base import SendOutcome, SendResult

            sent.append(address)
            return SendResult(outcome=SendOutcome.SENT, provider_message_id="1")

    import app.api.notifications as api_notifications

    monkeypatch.setattr(api_notifications, "channel_for", lambda name: FakeChannel())

    last = None
    for _ in range(settings.NOTIFY_OPT_IN_MAX_PER_HOUR + 1):
        last = await client.post(
            "/api/notifications/destinations/whatsapp/opt-in",
            json={"phone_number": "+6591234567"},
        )
    assert last.status_code == 429
    assert len(sent) == settings.NOTIFY_OPT_IN_MAX_PER_HOUR


async def test_opt_in_is_503_when_whatsapp_is_not_configured(
    client, signed_in, monkeypatch
) -> None:
    """Deliberate counterpart to test_opt_in_is_rate_limited: pins the
    unconfigured behaviour so it is asserted on purpose rather than only
    ever observed by accident, which is exactly how CI caught the missing
    keys and a local run sourcing .env.test did not.
    """
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")

    response = await client.post(
        "/api/notifications/destinations/whatsapp/opt-in",
        json={"phone_number": "+6591234567"},
    )
    assert response.status_code == 503


async def test_verify_is_rate_limited(client, signed_in, monkeypatch) -> None:
    """A six-digit code can be brute-forced within its TTL without this.

    RLS keeps a guess from reaching another tenant's code, but nothing stops
    a same-tenant actor from hammering a colleague's live code, so wrong
    guesses must be capped per user just like opt-in requests are.

    The pool is faked (never a real Redis) so this asserts the count the
    counter itself produces, not whatever a local Redis happened to have left
    over from a previous run.
    """
    monkeypatch.setattr(queue, "_create_pool", _fake_pool(_FakeRedis()))

    last = None
    for _ in range(settings.NOTIFY_VERIFY_MAX_PER_HOUR + 1):
        last = await client.post(
            "/api/notifications/destinations/whatsapp/verify",
            json={"code": "000000"},
        )
    assert last.status_code == 429


async def test_verify_attempts_are_counted_in_a_store_shared_across_instances(
    monkeypatch,
) -> None:
    """Pins Finding 1 directly: the old in-process dict would pass a test that
    only ever drove one app instance, which is exactly what
    `test_verify_is_rate_limited` above does and exactly why it could not
    have caught the bug.

    This asserts against the shared store itself (a key exists, with a TTL),
    and proves the count survives the API process's *local* pool cache being
    dropped and reopened — standing in for what a second Koyeb instance, or a
    restart of this one, would look like. If the counter still lived in a
    Python dict, dropping the pool cache would be irrelevant either way; the
    only way this test can fail is if the count itself is *not* shared state.
    """
    from app.services.notify.linking import record_verify_attempt

    fake = _FakeRedis()
    monkeypatch.setattr(queue, "_create_pool", _fake_pool(fake))

    user_id = uuid.uuid4()
    assert await record_verify_attempt(user_id) == 1

    # Simulate a second instance (or this one restarting) attaching to the
    # same Redis: only the local pool handle is gone, not the counter.
    queue.reset_pool()
    assert await record_verify_attempt(user_id) == 2

    key = f"notify:verify_attempts:{user_id}"
    assert fake.store[key] == 2
    assert fake.ttls[key] == settings.NOTIFY_VERIFY_WINDOW_SECONDS


def _fake_pool(fake: _FakeRedis):
    async def _create():
        return fake

    return _create


async def test_mine_is_true_for_the_callers_own_destination(
    client, signed_in, admin_session
) -> None:
    """`scope: "user"` alone can't tell the caller's own device from a
    colleague's — `mine` is the field the setup-nudge checklist keys off."""
    tenant_id, user_id = signed_in
    dest_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'x', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.commit()

    response = await client.get("/api/notifications/settings")
    destination = response.json()["destinations"][0]
    assert destination["scope"] == "user"
    assert destination["mine"] is True


async def test_mine_is_false_for_a_tenant_scoped_destination(
    client, signed_in, admin_session
) -> None:
    tenant_id, _user_id = signed_in
    dest_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :ch, 'x', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.commit()

    response = await client.get("/api/notifications/settings")
    destination = response.json()["destinations"][0]
    assert destination["scope"] == "tenant"
    assert destination["mine"] is False


async def test_mine_is_false_for_a_colleagues_personal_destination(
    client, signed_in, admin_session
) -> None:
    tenant_id, _user_id = signed_in
    colleague_id = uuid.uuid4()
    dest_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'colleague@a.sg', 'recruiter')"
        ),
        {"id": colleague_id, "tid": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'x', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": colleague_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("54321"),
        },
    )
    await admin_session.commit()

    response = await client.get("/api/notifications/settings")
    destination = response.json()["destinations"][0]
    assert destination["scope"] == "user"
    assert destination["mine"] is False


async def test_a_destination_can_be_promoted_to_the_whole_agency(
    client, signed_in, admin_session
) -> None:
    tenant_id, user_id = signed_in
    dest_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'x', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.commit()

    response = await client.put(
        f"/api/notifications/destinations/{dest_id}/scope", json={"scope": "tenant"}
    )
    assert response.status_code == 200

    settings_response = await client.get("/api/notifications/settings")
    assert settings_response.json()["destinations"][0]["scope"] == "tenant"


async def test_scope_cannot_reach_another_tenants_destination(
    client, signed_in, admin_session
) -> None:
    other_tenant, other_dest = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'other', :slug)"),
        {"id": other_tenant, "slug": f"other-{other_tenant.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :ch, 'x', :hash, now())"
        ),
        {
            "id": other_dest,
            "tid": other_tenant,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("88888"),
        },
    )
    await admin_session.commit()
    try:
        response = await client.put(
            f"/api/notifications/destinations/{other_dest}/scope",
            json={"scope": "tenant"},
        )
        assert response.status_code == 404
    finally:
        await admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": other_tenant}
        )
        await admin_session.commit()


async def _insert_wa_session(
    admin_session, tenant_id, user_id, status: str, phone_e164: str | None = None
) -> None:
    await admin_session.execute(
        text(
            "INSERT INTO wa_sessions (id, tenant_id, user_id, status, phone_e164) "
            "VALUES (:id, :tid, :uid, :status, :phone)"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tenant_id,
            "uid": user_id,
            "status": status,
            "phone": phone_e164,
        },
    )
    await admin_session.commit()


async def test_whatsapp_linked_is_true_only_when_connected(
    client, signed_in, admin_session
) -> None:
    tenant_id, user_id = signed_in
    await _insert_wa_session(
        admin_session, tenant_id, user_id, STATUS_CONNECTED, "+6591234567"
    )

    response = await client.get("/api/notifications/settings")
    body = response.json()
    assert body["channels"][CHANNEL_WHATSAPP_LINKED] is True
    assert body["whatsapp_linked_number"] == "+6591234567"


@pytest.mark.parametrize(
    "status", [STATUS_PAIRING, STATUS_RECONNECTING, STATUS_DISCONNECTED, STATUS_LOGGED_OUT]
)
async def test_whatsapp_linked_is_false_when_not_connected(
    client, signed_in, admin_session, status
) -> None:
    tenant_id, user_id = signed_in
    await _insert_wa_session(admin_session, tenant_id, user_id, status, "+6591234567")

    response = await client.get("/api/notifications/settings")
    body = response.json()
    assert body["channels"][CHANNEL_WHATSAPP_LINKED] is False
    assert body["whatsapp_linked_number"] is None


async def test_whatsapp_linked_is_false_with_no_session_row(client, signed_in) -> None:
    response = await client.get("/api/notifications/settings")
    body = response.json()
    assert body["channels"][CHANNEL_WHATSAPP_LINKED] is False
    assert body["whatsapp_linked_number"] is None


async def test_a_colleagues_connected_session_does_not_flip_it_for_the_caller(
    client, signed_in, admin_session
) -> None:
    """Per-user flag on a tenant-scoped read — exactly the thing that gets
    written wrong: a query that forgets the `user_id` predicate would show
    every recruiter in the agency as connected the moment any one of them is."""
    tenant_id, _user_id = signed_in
    colleague_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'colleague@a.sg', 'recruiter')"
        ),
        {"id": colleague_id, "tid": tenant_id},
    )
    await admin_session.commit()
    await _insert_wa_session(
        admin_session, tenant_id, colleague_id, STATUS_CONNECTED, "+6598765432"
    )

    response = await client.get("/api/notifications/settings")
    body = response.json()
    assert body["channels"][CHANNEL_WHATSAPP_LINKED] is False
    assert body["whatsapp_linked_number"] is None


async def test_whatsapp_linked_link_succeeds_when_connected(
    client, signed_in, admin_session
) -> None:
    tenant_id, user_id = signed_in
    await _insert_wa_session(
        admin_session, tenant_id, user_id, STATUS_CONNECTED, "+6591234567"
    )

    response = await client.post("/api/notifications/destinations/whatsapp-linked")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "linked"
    dest_id = body["destination_id"]

    row = (
        await admin_session.execute(
            text(
                "SELECT channel, user_id, verified_at, address_hash "
                "FROM notification_destinations WHERE id = :id"
            ),
            {"id": dest_id},
        )
    ).one()
    assert row.channel == CHANNEL_WHATSAPP_LINKED
    assert row.user_id == user_id
    assert row.verified_at is not None
    assert row.address_hash == address_digest("+6591234567")


async def test_whatsapp_linked_link_400_when_no_session(client, signed_in) -> None:
    response = await client.post("/api/notifications/destinations/whatsapp-linked")
    assert response.status_code == 400


@pytest.mark.parametrize(
    "status", [STATUS_PAIRING, STATUS_RECONNECTING, STATUS_DISCONNECTED, STATUS_LOGGED_OUT]
)
async def test_whatsapp_linked_link_400_when_not_connected(
    client, signed_in, admin_session, status
) -> None:
    tenant_id, user_id = signed_in
    await _insert_wa_session(admin_session, tenant_id, user_id, status, "+6591234567")

    response = await client.post("/api/notifications/destinations/whatsapp-linked")
    assert response.status_code == 400


async def test_whatsapp_linked_link_is_idempotent(
    client, signed_in, admin_session
) -> None:
    tenant_id, user_id = signed_in
    await _insert_wa_session(
        admin_session, tenant_id, user_id, STATUS_CONNECTED, "+6591234567"
    )

    first = await client.post("/api/notifications/destinations/whatsapp-linked")
    second = await client.post("/api/notifications/destinations/whatsapp-linked")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["destination_id"] == second.json()["destination_id"]

    count = (
        await admin_session.execute(
            text(
                "SELECT count(*) FROM notification_destinations "
                "WHERE tenant_id = :tid AND channel = :ch"
            ),
            {"tid": tenant_id, "ch": CHANNEL_WHATSAPP_LINKED},
        )
    ).scalar_one()
    assert count == 1


async def test_the_unique_index_backing_idempotency_exists(admin_session) -> None:
    """The 200-on-retry behaviour above relies on an upsert against
    `uq_destination_address` (tenant_id, channel, address_hash) in
    `create_destination` — this pins that the index the implementation is
    actually built on is really there, not just that the observable behaviour
    happens to look idempotent for some other accidental reason."""
    row = (
        await admin_session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'uq_destination_address'"
            )
        )
    ).one_or_none()
    assert row is not None
    assert "tenant_id" in row.indexdef
    assert "channel" in row.indexdef
    assert "address_hash" in row.indexdef


async def test_whatsapp_linked_destination_is_invisible_to_another_tenant(
    client, signed_in, admin_session
) -> None:
    tenant_id, user_id = signed_in
    await _insert_wa_session(
        admin_session, tenant_id, user_id, STATUS_CONNECTED, "+6591234567"
    )
    response = await client.post("/api/notifications/destinations/whatsapp-linked")
    dest_id = response.json()["destination_id"]

    other_tenant = uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'other', :slug)"),
        {"id": other_tenant, "slug": f"other-{other_tenant.hex[:8]}"},
    )
    await admin_session.commit()
    try:
        async with tenant_session(other_tenant) as session:
            row = (
                await session.execute(
                    text("SELECT id FROM notification_destinations WHERE id = :id"),
                    {"id": dest_id},
                )
            ).one_or_none()
        assert row is None
    finally:
        await admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": other_tenant}
        )
        await admin_session.commit()


async def test_scope_400s_for_a_whatsapp_linked_destination(
    client, signed_in, admin_session
) -> None:
    """Promoting a linked device to the agency would null the `user_id` the
    send path needs to find whose socket to use."""
    tenant_id, user_id = signed_in
    await _insert_wa_session(
        admin_session, tenant_id, user_id, STATUS_CONNECTED, "+6591234567"
    )
    response = await client.post("/api/notifications/destinations/whatsapp-linked")
    dest_id = response.json()["destination_id"]

    scope_response = await client.put(
        f"/api/notifications/destinations/{dest_id}/scope", json={"scope": "tenant"}
    )
    assert scope_response.status_code == 400
