"""`/api/wa/*` (spec 2026-07-29-baileys-gateway-plan.md, §4-§6).

`WaGatewayClient` is monkeypatched rather than hit over real HTTP: these
routes are thin proxies, and what they must get right is auth, §18 scoping,
and never 500ing on a dead gateway — none of which needs a running gateway
process to prove.
"""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.wa_gateway import GatewayUnreachableError, SessionSnapshot


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
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await admin_session.commit()


@pytest.fixture(autouse=True)
def configured_gateway(monkeypatch):
    """Point `wa_gateway_configured()` at a fake URL/secret for every test in
    this file, so `_call_gateway` doesn't short-circuit to `gateway_unreachable`
    before the monkeypatched client is even reached."""
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "http://gateway.internal:7300")
    monkeypatch.setattr(settings, "WA_GATEWAY_SHARED_SECRET", "test-shared-secret")


async def test_start_session_requires_a_session(client) -> None:
    response = await client.post("/api/wa/session")
    assert response.status_code == 401


async def test_get_session_requires_a_session(client) -> None:
    response = await client.get("/api/wa/session")
    assert response.status_code == 401


async def test_disconnect_requires_a_session(client) -> None:
    response = await client.post("/api/wa/session/disconnect")
    assert response.status_code == 401


async def test_get_session_returns_the_gateways_snapshot(client, signed_in, monkeypatch) -> None:
    seen: dict = {}

    async def fake_status(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        seen["tenant_id"] = tenant_id
        seen["user_id"] = user_id
        return SessionSnapshot(status="connected", phone_number="6591234567")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.status", fake_status)

    tenant_id, user_id = signed_in
    response = await client.get("/api/wa/session")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "connected"
    assert body["phone_number"] == "6591234567"
    # "when we last looked", not "when WhatsApp last saw the user" (§15) —
    # present and a real timestamp, but never named last_seen_at.
    assert body["last_checked_at"]
    assert "last_seen_at" not in body
    # §18: the ids the gateway is asked about come from the session cookie,
    # never from anything the request itself could supply.
    assert seen == {"tenant_id": str(tenant_id), "user_id": str(user_id)}


async def test_pair_ignores_ids_in_the_request_body(client, signed_in, monkeypatch) -> None:
    """§18: even if a client tried to smuggle another tenant's ids into the
    body, there is nowhere in this route for them to land — it never reads
    the request body at all, only the cookie."""

    async def fake_pair(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        assert tenant_id == str(signed_in[0])
        assert user_id == str(signed_in[1])
        return SessionSnapshot(status="pairing", qr="abc123", expires_at="2026-07-29T00:00:20Z")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.pair", fake_pair)

    response = await client.post(
        "/api/wa/session",
        json={"tenant_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4())},
    )
    assert response.status_code == 200
    assert response.json()["qr"] == "abc123"


async def test_a_dead_gateway_is_a_status_not_a_500(client, signed_in, monkeypatch) -> None:
    async def fake_status(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        raise GatewayUnreachableError("boom")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.status", fake_status)

    response = await client.get("/api/wa/session")
    assert response.status_code == 200
    assert response.json()["status"] == "gateway_unreachable"


async def test_unconfigured_gateway_is_also_a_status_not_a_crash(
    client, signed_in, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "")
    response = await client.get("/api/wa/session")
    assert response.status_code == 200
    assert response.json()["status"] == "gateway_unreachable"


async def test_disconnect_answers_disconnected_even_if_the_gateway_is_unreachable(
    client, signed_in, monkeypatch
) -> None:
    async def fake_disconnect(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        raise GatewayUnreachableError("boom")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.disconnect", fake_disconnect)

    response = await client.post("/api/wa/session/disconnect")
    assert response.status_code == 200
    assert response.json() == {"status": "disconnected"}


async def test_shared_secret_never_appears_in_a_response_body(
    client, signed_in, monkeypatch
) -> None:
    async def fake_status(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        raise GatewayUnreachableError("boom")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.status", fake_status)

    response = await client.get("/api/wa/session")
    assert settings.WA_GATEWAY_SHARED_SECRET not in response.text
    assert settings.WA_GATEWAY_URL not in response.text


async def test_internal_status_requires_the_shared_secret(client) -> None:
    response = await client.post(
        "/api/wa/internal/status",
        json={
            "tenant_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "status": "connected",
        },
    )
    assert response.status_code == 401


async def test_internal_status_rejects_a_wrong_secret(client) -> None:
    response = await client.post(
        "/api/wa/internal/status",
        headers={"Authorization": "Bearer definitely-wrong"},
        json={
            "tenant_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "status": "connected",
        },
    )
    assert response.status_code == 401


async def test_internal_status_writes_the_row_and_is_readable_back_via_get_session(
    client, signed_in, monkeypatch
) -> None:
    tenant_id, user_id = signed_in

    response = await client.post(
        "/api/wa/internal/status",
        headers={"Authorization": f"Bearer {settings.WA_GATEWAY_SHARED_SECRET}"},
        json={
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "status": "connected",
            "phone_e164": "6591234567",
        },
    )
    assert response.status_code == 200

    async def fake_status(self, tid: str, uid: str) -> SessionSnapshot:
        return SessionSnapshot(status="connected", phone_number="6591234567")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.status", fake_status)
    again = await client.get("/api/wa/session")
    assert again.json()["status"] == "connected"
