"""`/api/wa/consent` and the pairing gate (plan §9, onboarding gap).

`test_wa_gateway_api.py` covers the general shape of `/api/wa/session`; this
file covers the risk-notice acknowledgement specifically — the 422 for a
stale/unknown version, the 409 pairing gate, the version/text relationship,
and §18 cross-tenant isolation of the acknowledgement itself.
"""

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.wa_risk_notice import NOTICE_TEXT, NOTICE_VERSION, _content_version


@pytest.fixture
async def client() -> httpx.AsyncClient:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


async def _agency(admin_session, client) -> tuple[uuid.UUID, uuid.UUID]:
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
    return tenant_id, user_id


@pytest.fixture
async def signed_in(admin_session, client):
    tenant_id, user_id = await _agency(admin_session, client)
    yield tenant_id, user_id
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await admin_session.commit()


@pytest.fixture(autouse=True)
def configured_gateway(monkeypatch):
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "http://gateway.internal:7300")
    monkeypatch.setattr(settings, "WA_GATEWAY_SHARED_SECRET", "test-shared-secret")


def test_the_version_is_a_hash_of_the_text_not_a_hand_maintained_string() -> None:
    """Pins the enforcement mechanism itself: editing the text without
    touching the version constant must still change what `NOTICE_VERSION`
    evaluates to, because it is derived, not typed by hand."""
    assert NOTICE_VERSION == _content_version(NOTICE_TEXT)
    assert _content_version(NOTICE_TEXT + " ") != NOTICE_VERSION


async def test_get_session_reports_no_acknowledgement_for_a_fresh_user(client, signed_in) -> None:
    response = await client.get("/api/wa/session")
    assert response.status_code == 200
    body = response.json()
    assert body["risk_acknowledged_at"] is None
    assert body["risk_notice_version"] is None
    assert body["notice_version"] == NOTICE_VERSION
    assert body["notice_text"] == NOTICE_TEXT


async def test_consent_requires_a_session(client) -> None:
    response = await client.post("/api/wa/consent", json={"notice_version": NOTICE_VERSION})
    assert response.status_code == 401


async def test_consent_records_the_current_version(client, signed_in) -> None:
    response = await client.post("/api/wa/consent", json={"notice_version": NOTICE_VERSION})
    assert response.status_code == 200
    body = response.json()
    assert body["risk_notice_version"] == NOTICE_VERSION
    assert body["risk_acknowledged_at"]

    again = await client.get("/api/wa/session")
    got = again.json()
    assert got["risk_notice_version"] == NOTICE_VERSION
    assert got["risk_acknowledged_at"] == body["risk_acknowledged_at"]


async def test_acknowledging_a_stale_version_is_422_and_records_nothing(
    client, signed_in
) -> None:
    stale = _content_version(NOTICE_TEXT + " (an earlier wording)")
    response = await client.post("/api/wa/consent", json={"notice_version": stale})
    assert response.status_code == 422

    after = await client.get("/api/wa/session")
    body = after.json()
    assert body["risk_acknowledged_at"] is None
    assert body["risk_notice_version"] is None


async def test_acknowledging_an_unknown_version_is_422(client, signed_in) -> None:
    response = await client.post("/api/wa/consent", json={"notice_version": "not-a-real-version"})
    assert response.status_code == 422


async def test_pairing_is_refused_before_acknowledgement(client, signed_in, monkeypatch) -> None:
    from app.services.wa_gateway import SessionSnapshot

    async def fake_pair(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        raise AssertionError("must not reach the gateway before acknowledgement")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.pair", fake_pair)

    response = await client.post("/api/wa/session")
    assert response.status_code == 409
    body = response.json()
    assert body["reason"] == "risk_not_acknowledged"
    assert set(body.keys()) == {"detail", "reason"}


async def test_pairing_is_permitted_after_acknowledgement(client, signed_in, monkeypatch) -> None:
    from app.services.wa_gateway import SessionSnapshot

    async def fake_pair(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        return SessionSnapshot(status="pairing", qr="abc123", expires_at="2026-07-29T00:00:20Z")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.pair", fake_pair)

    ack = await client.post("/api/wa/consent", json={"notice_version": NOTICE_VERSION})
    assert ack.status_code == 200

    response = await client.post("/api/wa/session")
    assert response.status_code == 200
    assert response.json()["qr"] == "abc123"


async def test_an_existing_session_with_no_acknowledgement_is_not_treated_as_consented(
    admin_session, client, signed_in, monkeypatch
) -> None:
    """A `wa_sessions` row that predates this feature — created e.g. by the
    gateway's internal status callback — must not be read as consent just
    because the row exists."""
    from app.services.wa_gateway import SessionSnapshot

    tenant_id, user_id = signed_in
    await admin_session.execute(
        text(
            "INSERT INTO wa_sessions (id, tenant_id, user_id, status) "
            "VALUES (:id, :tid, :uid, 'disconnected')"
        ),
        {"id": user_id, "tid": tenant_id, "uid": user_id},
    )
    await admin_session.commit()

    async def fake_pair(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        raise AssertionError("an existing row with no acknowledgement is not consent")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.pair", fake_pair)

    response = await client.post("/api/wa/session")
    assert response.status_code == 409
    assert response.json()["reason"] == "risk_not_acknowledged"


async def test_agency_b_cannot_acknowledge_for_agency_a(admin_session, client) -> None:
    """§18: consent is written to the signed-in caller's own row; there is no
    request field that could target another tenant's session even if a
    caller tried."""
    tenant_a, user_a = await _agency(admin_session, client)

    ack = await client.post("/api/wa/consent", json={"notice_version": NOTICE_VERSION})
    assert ack.status_code == 200

    row = (
        await admin_session.execute(
            text("SELECT tenant_id, user_id FROM wa_sessions WHERE user_id = :uid"),
            {"uid": user_a},
        )
    ).one()
    assert row.tenant_id == tenant_a
    assert row.user_id == user_a

    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_a})
    await admin_session.commit()


async def test_agency_b_cannot_read_agency_as_acknowledgement(admin_session, client) -> None:
    """§18: `GET /api/wa/session` for agency B reports no acknowledgement even
    though agency A has one — the RLS-scoped read never crosses tenants."""
    tenant_a, user_a = await _agency(admin_session, client)
    ack = await client.post("/api/wa/consent", json={"notice_version": NOTICE_VERSION})
    assert ack.status_code == 200

    # Switch the same client to a fresh agency B session.
    tenant_b, user_b = await _agency(admin_session, client)
    got = await client.get("/api/wa/session")
    body = got.json()
    assert body["risk_acknowledged_at"] is None
    assert body["risk_notice_version"] is None

    await admin_session.execute(
        text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b}
    )
    await admin_session.commit()
