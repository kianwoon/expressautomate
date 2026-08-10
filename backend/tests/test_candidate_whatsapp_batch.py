"""Batch WhatsApp outreach: the shared draft, and the per-candidate greeting.

The batch flow reuses the single-send endpoint (`test_candidate_whatsapp_send.py`
covers the send pipeline itself — spacing, cap, idempotency, RLS, gateway
outcomes). What is new here is the *batch shape*:

- `GET /whatsapp-batch-draft` renders the shared body — the outreach template
  minus the greeting line, since a batch greets each candidate by name at send
  time.
- `POST /candidates/{id}/whatsapp-send` with `prepend_greeting: true` takes
  that shared body and prepends `Hi {candidate_name},` from the candidate's
  own row — never from the request, so a client cannot fabricate a greeting.

Both must keep the same honesty the single flow does: the draft names the
recruiter and agency from the session, the body is shared and editable, and a
prepended greeting is exactly the name on the candidate's row.

allow-hardcode: the SQL below is test fixture data, written out rather than
built through the ORM so that a schema change breaks these tests loudly.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import settings
from app.main import app
from app.services.wa_gateway import SendOutcome
from app.services.wa_risk_notice import NOTICE_VERSION
from tests.conftest import AdminSessionLocal
from tests.test_clients_api import sign_in

SECRET = "test-shared-secret-never-in-a-response"
BATCH_DRAFT = "/api/whatsapp-batch-draft"
SEND = "/api/candidates/{cid}/whatsapp-send"


@pytest.fixture(autouse=True)
def configured_gateway(monkeypatch):
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "http://gateway.internal:7300")
    monkeypatch.setattr(settings, "WA_GATEWAY_SHARED_SECRET", SECRET)


@pytest.fixture
async def agency():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, display_name, role) "
                "VALUES (:i, :t, :e, 'Wong', 'owner')"
            ),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO wa_sessions (id, tenant_id, user_id, status, "
                "ban_risk_acknowledged_at, risk_notice_version) "
                "VALUES (:i, :t, :u, 'disconnected', now(), :v)"
            ),
            {"i": uid, "t": tid, "u": uid, "v": NOTICE_VERSION},
        )
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, phone_e164, "
                "current_title) VALUES (:i, :t, :n, :p, 'Engineer')"
            ),
            {"i": cid, "t": tid, "n": "Hui Ling Tan", "p": "+6582217734"},
        )
        await s.commit()
    yield tid, uid, cid
    async with AdminSessionLocal() as s:
        for table in ("candidate_activities", "candidates", "users"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


def _fake_client(monkeypatch, *, outcome=None, record=None):
    class Fake:
        async def send(self, tenant_id, user_id, *, to, text):
            if record is not None:
                record.append({"to": to, "text": text})
            return outcome or SendOutcome(
                ok=True, session_status="connected", provider_message_id="WAMSG-9"
            )

    monkeypatch.setattr("app.api.candidate_whatsapp.WaGatewayClient", Fake)


async def test_batch_draft_requires_a_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.get(BATCH_DRAFT)
    assert resp.status_code == 401


async def test_batch_draft_renders_the_shared_body_without_a_greeting(agency) -> None:
    tid, uid, _ = agency
    async with await _client_for(tid, uid) as http:
        resp = await http.get(BATCH_DRAFT)
        assert resp.status_code == 200, resp.text
        message = resp.json()["message"]

    # The recruiter and agency come from the session, exactly as the single
    # draft renders them.
    assert message.startswith("This is Wong from ")
    assert "Would you be available for a quick discussion?" in message
    # The whole point of the batch body: no greeting line, because each
    # candidate is greeted by name at send time.
    assert "Hi " not in message


async def test_batch_draft_uses_the_job_title_when_given(agency) -> None:
    tid, uid, _ = agency
    async with await _client_for(tid, uid) as http:
        resp = await http.get(BATCH_DRAFT, params={"job_title": "Senior Engineer"})
        assert resp.status_code == 200, resp.text
        message = resp.json()["message"]

    assert "regarding a Senior Engineer opportunity" in message


async def test_batch_draft_without_a_title_rewrites_the_interest_line(agency) -> None:
    tid, uid, _ = agency
    async with await _client_for(tid, uid) as http:
        resp = await http.get(BATCH_DRAFT)
        assert resp.status_code == 200, resp.text

    assert "regarding an opportunity" in resp.json()["message"]
    assert "regarding a  opportunity" not in resp.json()["message"]


async def test_prepend_greeting_sends_hi_name_then_the_shared_body(agency, monkeypatch) -> None:
    tid, uid, cid = agency
    seen: list[dict] = []
    _fake_client(monkeypatch, record=seen)
    async with await _client_for(tid, uid) as http:
        resp = await http.post(
            SEND.format(cid=cid),
            json={
                "message": "This is Wong from agency.\n\nWould you be available?",
                "client_request_id": str(uuid.uuid4()),
                "prepend_greeting": True,
            },
        )
        assert resp.status_code == 200, resp.text

    # The gateway got the candidate's own name on the greeting line, composed
    # server-side — the client only sent the shared body.
    assert seen == [
        {
            "to": "+6582217734",
            "text": "Hi Hui Ling Tan,\n\nThis is Wong from agency.\n\nWould you be available?",
        }
    ]


async def test_prepend_greeting_requires_a_message(agency) -> None:
    tid, uid, cid = agency
    async with await _client_for(tid, uid) as http:
        resp = await http.post(
            SEND.format(cid=cid),
            json={"client_request_id": str(uuid.uuid4()), "prepend_greeting": True},
        )
    # 422, not a fallback to the rendered draft: a greeting with no body is a
    # message nobody chose, and silently rendering one would send words the
    # recruiter never reviewed.
    assert resp.status_code == 422
    assert "message body is required" in resp.json()["detail"]


async def test_prepend_greeting_never_accepts_a_client_supplied_name(agency, monkeypatch) -> None:
    """The greeting name is the candidate row's, even if the request tries to
    sneak a different name in via the body. The shared body is edited verbatim
    — the server's prepend is the only greeting there is."""
    tid, uid, cid = agency
    seen: list[dict] = []
    _fake_client(monkeypatch, record=seen)
    async with await _client_for(tid, uid) as http:
        resp = await http.post(
            SEND.format(cid=cid),
            json={
                "message": "Hi Fake Name,\n\nI would like to speak with you.",
                "client_request_id": str(uuid.uuid4()),
                "prepend_greeting": True,
            },
        )
        assert resp.status_code == 200, resp.text

    # The body is not treated as a greeting carrier — the prepended line comes
    # from the candidate row and the body is the body.
    assert seen[0]["text"].startswith("Hi Hui Ling Tan,\n\nHi Fake Name,")
