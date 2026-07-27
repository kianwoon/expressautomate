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
from app.models.notification import CHANNEL_TELEGRAM, address_digest
from app.services.notify.events import ALL_EVENT_KINDS, EVENT_OPPORTUNITY_NEW


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


async def test_telegram_link_returns_a_deep_link(client, signed_in) -> None:
    response = await client.post("/api/notifications/destinations/telegram/link")
    assert response.status_code == 200
    assert response.json()["url"].startswith("https://t.me/")


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


async def test_opt_in_is_rate_limited(client, signed_in, monkeypatch) -> None:
    """Otherwise this endpoint is an OTP pump on our WABA's reputation."""
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


async def test_verify_is_rate_limited(client, signed_in) -> None:
    """A six-digit code can be brute-forced within its TTL without this.

    RLS keeps a guess from reaching another tenant's code, but nothing stops
    a same-tenant actor from hammering a colleague's live code, so wrong
    guesses must be capped per user just like opt-in requests are.
    """
    last = None
    for _ in range(settings.NOTIFY_VERIFY_MAX_PER_HOUR + 1):
        last = await client.post(
            "/api/notifications/destinations/whatsapp/verify",
            json={"code": "000000"},
        )
    assert last.status_code == 429


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
