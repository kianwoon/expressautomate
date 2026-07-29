"""`POST /api/candidates/{id}/whatsapp-send` — the WA gateway send path (§7-§8).

`WaGatewayClient` is monkeypatched rather than hit over HTTP, the same way
`test_wa_gateway_api.py` does it: what this route must get right is §18
scoping, the never-500 contract, idempotency, the daily cap, and **which rows
exist afterwards** — none of which needs a running gateway.

Three honesty rules under test, each one a claim the recruiter would act on:

- `sent` means the gateway's socket accepted the message and WhatsApp returned
  a message id. Not delivered, never read.
- `failed` requires a refusal we can quote; a lost response after dispatch is
  `unknown`, because the message may have gone out.
- A refusal *before* dispatch writes no row at all — nothing was attempted.

allow-hardcode: the SQL below is test fixture data, written out rather than
built through the ORM so that a schema change breaks these tests loudly.
Nothing here matches on phrases.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

import app.api.candidate_whatsapp as wa_api
from app.core.config import settings
from app.main import app
from app.services.wa_gateway import (
    GatewayOutcomeUnknownError,
    GatewayRefusedError,
    GatewayUnreachableError,
    SendOutcome,
)
from tests.conftest import AdminSessionLocal
from tests.test_clients_api import sign_in

SECRET = "test-shared-secret-never-in-a-response"
SEND = "/api/candidates/{cid}/whatsapp-send"


@pytest.fixture(autouse=True)
def configured_gateway(monkeypatch):
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "http://gateway.internal:7300")
    monkeypatch.setattr(settings, "WA_GATEWAY_SHARED_SECRET", SECRET)


@pytest.fixture
async def agency():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    ids = {"with_phone": uuid.uuid4(), "no_phone": uuid.uuid4()}
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
        for cid, name, phone in (
            (ids["with_phone"], "Hui Ling Tan", "+6582217734"),
            (ids["no_phone"], "No Phone Tan", None),
        ):
            await s.execute(
                text(
                    "INSERT INTO candidates (id, tenant_id, full_name, phone_e164, "
                    "current_title) VALUES (:i, :t, :n, :p, 'Engineer')"
                ),
                {"i": cid, "t": tid, "n": name, "p": phone},
            )
        await s.commit()
    yield tid, uid, ids
    async with AdminSessionLocal() as s:
        for table in ("candidate_activities", "candidates", "users"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


def _body(message: str | None = "Hi", key: uuid.UUID | None = None) -> dict:
    payload: dict = {"client_request_id": str(key or uuid.uuid4())}
    if message is not None:
        payload["message"] = message
    return payload


def _fake_client(monkeypatch, *, outcome=None, raises=None, record=None):
    """Swap `WaGatewayClient()` for one that answers however this test needs."""

    class Fake:
        async def send(self, tenant_id, user_id, *, to, text):
            if record is not None:
                record.append({"tenant_id": tenant_id, "user_id": user_id, "to": to, "text": text})
            if raises is not None:
                raise raises
            return outcome

    monkeypatch.setattr(wa_api, "WaGatewayClient", Fake)


async def _activities(tid, cid) -> list[dict]:
    async with AdminSessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT status, activity_type, provider_message_id, error, message_text "
                    "FROM candidate_activities WHERE tenant_id = :t AND candidate_id = :c "
                    "ORDER BY created_at"
                ),
                {"t": tid, "c": cid},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def test_send_requires_a_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.post(SEND.format(cid=uuid.uuid4()), json=_body())
    assert resp.status_code == 401


async def test_the_idempotency_key_is_required(agency) -> None:
    """Optional would mean absent exactly when a caller is retrying."""
    tid, uid, ids = agency
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json={"message": "Hi"})
    assert resp.status_code == 422


async def test_a_successful_send_records_sent_with_the_message_id(agency, monkeypatch) -> None:
    tid, uid, ids = agency
    seen: list[dict] = []
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="WAMSG-7"),
        record=seen,
    )
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body("Hi there"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "sent"
        assert body["provider_message_id"] == "WAMSG-7"

        timeline = (
            await http.get(f"/api/candidates/{ids['with_phone']}/activities")
        ).json()["items"]

    # The number came from the candidate row, not from the request (plan §9).
    assert seen == [
        {"tenant_id": str(tid), "user_id": str(uid), "to": "+6582217734", "text": "Hi there"}
    ]
    rows = await _activities(tid, ids["with_phone"])
    assert rows == [
        {
            "status": "sent",
            "activity_type": "whatsapp_sent",
            "provider_message_id": "WAMSG-7",
            "error": None,
            "message_text": "Hi there",
        }
    ]
    assert timeline[0]["id"] == body["activity_id"]
    assert timeline[0]["status"] == "sent"
    assert timeline[0]["provider_message_id"] == "WAMSG-7"


async def test_replaying_one_key_sends_once_and_returns_the_same_row(agency, monkeypatch) -> None:
    """The whole point of the key: a retried click is not a second message."""
    tid, uid, ids = agency
    seen: list[dict] = []
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="WAMSG-3"),
        record=seen,
    )
    key = uuid.uuid4()
    async with await _client_for(tid, uid) as http:
        first = await http.post(SEND.format(cid=ids["with_phone"]), json=_body("Hi", key))
        second = await http.post(SEND.format(cid=ids["with_phone"]), json=_body("Hi", key))

    assert first.status_code == 200 and second.status_code == 200
    assert len(seen) == 1, "the second call must not reach the gateway"
    assert first.json() == second.json()
    assert first.json()["client_request_id"] == str(key)
    assert len(await _activities(tid, ids["with_phone"])) == 1


async def test_a_replay_returns_the_original_even_if_the_text_changed(
    agency, monkeypatch
) -> None:
    """The key identifies the send, not the request body. A retry that arrives
    with different words still gets the outcome that actually happened."""
    tid, uid, ids = agency
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="WAMSG-4"),
    )
    key = uuid.uuid4()
    async with await _client_for(tid, uid) as http:
        await http.post(SEND.format(cid=ids["with_phone"]), json=_body("Original", key))
        replay = await http.post(SEND.format(cid=ids["with_phone"]), json=_body("Rewritten", key))

    assert replay.status_code == 200
    rows = await _activities(tid, ids["with_phone"])
    assert [row["message_text"] for row in rows] == ["Original"]


async def test_the_same_key_in_another_agency_is_not_a_replay(agency, monkeypatch) -> None:
    """The index is per tenant: a browser-minted value colliding across two
    agencies is a coincidence, and must not make B's send return A's row."""
    tid, uid, ids = agency
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="WAMSG-5"),
    )
    key = uuid.uuid4()
    async with await _client_for(tid, uid) as http:
        assert (
            await http.post(SEND.format(cid=ids["with_phone"]), json=_body("Hi", key))
        ).status_code == 200

    tid_b, uid_b, cid_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid_b, "n": f"agency-{tid_b.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid_b, "t": tid_b, "e": f"u{uid_b.hex[:6]}@other.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, phone_e164) "
                "VALUES (:i, :t, 'Their Candidate', '+6591112222')"
            ),
            {"i": cid_b, "t": tid_b},
        )
        await s.commit()
    try:
        async with await _client_for(tid_b, uid_b) as http:
            resp = await http.post(SEND.format(cid=cid_b), json=_body("Hi", key))
        assert resp.status_code == 200
        assert len(await _activities(tid_b, cid_b)) == 1
    finally:
        async with AdminSessionLocal() as s:
            for table in ("candidate_activities", "candidates", "users"):
                await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid_b})
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid_b})
            await s.commit()


async def test_omitting_the_message_sends_the_rendered_draft(agency, monkeypatch) -> None:
    """The send path and the popup path must put the same words on the wire —
    one template, fetched from the same function `GET /whatsapp-draft` uses."""
    tid, uid, ids = agency
    seen: list[dict] = []
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="WAMSG-1"),
        record=seen,
    )
    async with await _client_for(tid, uid) as http:
        drafted = (
            await http.get(f"/api/candidates/{ids['with_phone']}/whatsapp-draft")
        ).json()["message"]
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body(None))
    assert resp.status_code == 200
    assert seen[0]["text"] == drafted
    assert seen[0]["text"].startswith("Hi Hui Ling Tan,")


@pytest.mark.parametrize(
    "session_status", ["pairing", "reconnecting", "disconnected", "logged_out"]
)
async def test_each_unconnected_status_is_409_naming_it_and_writes_no_row(
    agency, monkeypatch, session_status
) -> None:
    """A refusal must say which status it is: "still reconnecting, wait" and
    "WhatsApp logged you out, pair again" need different next actions.

    And it writes nothing: the gateway declined before touching WhatsApp, so
    there is no attempt to record."""
    tid, uid, ids = agency
    _fake_client(monkeypatch, outcome=SendOutcome(ok=False, session_status=session_status))
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert resp.status_code == 409
    body = resp.json()
    assert body["session_status"] == session_status
    assert body["detail"] != ""
    assert await _activities(tid, ids["with_phone"]) == []


async def test_a_refused_message_is_failed_with_the_gateways_own_words(
    agency, monkeypatch
) -> None:
    """`failed` is only ever an observed refusal, and `error` quotes it
    verbatim — we do not paraphrase someone else's reason."""
    tid, uid, ids = agency
    refusal = "not-authorized: recipient has blocked this number"
    _fake_client(monkeypatch, raises=GatewayRefusedError(refusal))
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())

    assert resp.status_code == 409
    assert resp.json()["detail"] == refusal
    # The session itself was fine — do not send the recruiter to re-pair it.
    assert resp.json()["session_status"] == "connected"
    rows = await _activities(tid, ids["with_phone"])
    assert [row["status"] for row in rows] == ["failed"]
    assert rows[0]["error"] == refusal
    assert rows[0]["provider_message_id"] is None


async def test_a_lost_response_after_dispatch_is_unknown_never_failed(
    agency, monkeypatch
) -> None:
    """The message may well have gone out. Calling that `failed` would invite
    the recruiter to send it a second time."""
    tid, uid, ids = agency
    _fake_client(monkeypatch, raises=GatewayOutcomeUnknownError("the gateway stopped answering"))
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
        timeline = (
            await http.get(f"/api/candidates/{ids['with_phone']}/activities")
        ).json()["items"]

    # 200, not an error: something did happen, and the recruiter needs the
    # record rather than a message that reads as "try again".
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "unknown"
    assert resp.json()["provider_message_id"] is None

    rows = await _activities(tid, ids["with_phone"])
    assert [row["status"] for row in rows] == ["unknown"]
    assert "do not know" in rows[0]["error"]
    assert timeline[0]["status"] == "unknown"


async def test_a_gateway_that_never_connected_is_409_and_writes_no_row(
    agency, monkeypatch
) -> None:
    """Plan §7: a dead gateway degrades to the popup path, which already
    works — so it must never 500, and must never put the gateway's URL or the
    shared secret anywhere the browser can read. Nothing was dispatched, so
    nothing is recorded."""
    tid, uid, ids = agency
    _fake_client(monkeypatch, raises=GatewayUnreachableError("the WA gateway could not be reached"))
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())

    assert resp.status_code == 409
    assert resp.json()["session_status"] == "gateway_unreachable"
    assert SECRET not in resp.text
    assert "gateway.internal" not in resp.text
    assert await _activities(tid, ids["with_phone"]) == []


async def test_an_unexpected_client_error_is_unknown_and_never_leaks(agency, monkeypatch) -> None:
    """An `httpx.HTTPStatusError` carries the URL in its own string, so the
    catch-all matters twice: it keeps the never-500 contract and keeps the URL
    out. We cannot tell whether it dispatched, so it is `unknown`."""
    tid, uid, ids = agency
    _fake_client(monkeypatch, raises=RuntimeError(f"boom at http://gateway.internal:7300 {SECRET}"))
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert resp.status_code == 200
    assert resp.json()["status"] == "unknown"
    assert SECRET not in resp.text
    assert "gateway.internal" not in resp.text
    rows = await _activities(tid, ids["with_phone"])
    assert SECRET not in (rows[0]["error"] or "")
    assert "gateway.internal" not in (rows[0]["error"] or "")


async def test_an_unconfigured_gateway_is_409_rather_than_a_500(agency, monkeypatch) -> None:
    """A brand-new Koyeb service starts with zero env vars — CLAUDE.md's
    GRAPH_BASE_URL / R2_* precedent. That is a deployment state, not a crash."""
    tid, uid, ids = agency
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "")
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert resp.status_code == 409
    assert resp.json()["session_status"] == "gateway_unreachable"
    assert await _activities(tid, ids["with_phone"]) == []


async def test_no_phone_is_422_with_no_session_status_and_no_row(agency, monkeypatch) -> None:
    """A missing number is a data problem, not a session state. Naming a
    session status here would send someone to Settings to fix a connection
    that is not broken."""
    tid, uid, ids = agency
    called: list[dict] = []
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="X"),
        record=called,
    )
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["no_phone"]), json=_body())
    assert resp.status_code == 422
    body = resp.json()
    assert "mobile number" in body["detail"]
    assert "session_status" not in body
    assert called == [], "the gateway must not be called for a candidate WhatsApp cannot reach"
    assert await _activities(tid, ids["no_phone"]) == []


async def _seed_sent_today(tid, uid, cid, count: int) -> None:
    """`count` of this recruiter's own sends, timestamped inside today's UTC day."""
    stamp = datetime.now(UTC).replace(hour=0, minute=5, second=0, microsecond=0)
    async with AdminSessionLocal() as s:
        for _ in range(count):
            await s.execute(
                text(
                    "INSERT INTO candidate_activities (id, tenant_id, candidate_id, user_id, "
                    "activity_type, channel, status, provider_message_id, created_at, "
                    "updated_at) VALUES (:i, :t, :c, :u, 'whatsapp_sent', 'whatsapp', 'sent', "
                    "'seed', :ts, :ts)"
                ),
                {"i": uuid.uuid4(), "t": tid, "c": cid, "u": uid, "ts": stamp},
            )
        await s.commit()


async def test_the_daily_cap_is_429_shaped_like_the_409_fallback(agency, monkeypatch) -> None:
    """Plan §9's ban-risk rail. The body carries `detail` exactly as the 409s
    do, so the modal's fall-back-to-popup branch handles both without a second
    code path — and nothing was attempted, so nothing is recorded."""
    tid, uid, ids = agency
    monkeypatch.setattr(settings, "WA_SEND_DAILY_LIMIT", 2)
    await _seed_sent_today(tid, uid, ids["with_phone"], 2)
    called: list[dict] = []
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="X"),
        record=called,
    )
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())

    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and detail
    assert "2" in detail, "the recruiter is told what the limit actually is"
    assert "WhatsApp Web" in detail, "and what to do instead"
    assert called == []
    # Only the seeded rows — the refused attempt added nothing.
    assert len(await _activities(tid, ids["with_phone"])) == 2


async def test_one_send_below_the_cap_still_goes_out(agency, monkeypatch) -> None:
    tid, uid, ids = agency
    monkeypatch.setattr(settings, "WA_SEND_DAILY_LIMIT", 3)
    await _seed_sent_today(tid, uid, ids["with_phone"], 2)
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="WAMSG-8"),
    )
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"


async def test_yesterdays_sends_do_not_count_against_today(agency, monkeypatch) -> None:
    """The window is the UTC day, not a rolling total."""
    tid, uid, ids = agency
    monkeypatch.setattr(settings, "WA_SEND_DAILY_LIMIT", 1)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_activities (id, tenant_id, candidate_id, user_id, "
                "activity_type, channel, status, created_at, updated_at) "
                "VALUES (:i, :t, :c, :u, 'whatsapp_sent', 'whatsapp', 'sent', :ts, :ts)"
            ),
            {"i": uuid.uuid4(), "t": tid, "c": ids["with_phone"], "u": uid, "ts": yesterday},
        )
        await s.commit()
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="WAMSG-2"),
    )
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert resp.status_code == 200


async def test_failed_and_unknown_rows_do_not_consume_the_allowance(agency, monkeypatch) -> None:
    """A recruiter must not lose part of their day to our uncertainty."""
    tid, uid, ids = agency
    monkeypatch.setattr(settings, "WA_SEND_DAILY_LIMIT", 1)
    stamp = datetime.now(UTC).replace(hour=0, minute=5, second=0, microsecond=0)
    async with AdminSessionLocal() as s:
        for status in ("failed", "unknown"):
            await s.execute(
                text(
                    "INSERT INTO candidate_activities (id, tenant_id, candidate_id, user_id, "
                    "activity_type, channel, status, error, created_at, updated_at) "
                    "VALUES (:i, :t, :c, :u, 'whatsapp_sent', 'whatsapp', :s, 'x', :ts, :ts)"
                ),
                {
                    "i": uuid.uuid4(),
                    "t": tid,
                    "c": ids["with_phone"],
                    "u": uid,
                    "s": status,
                    "ts": stamp,
                },
            )
        await s.commit()
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="WAMSG-6"),
    )
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert resp.status_code == 200


async def test_every_refusal_body_is_flat_never_nested_under_detail(
    agency, monkeypatch
) -> None:
    """The one shape the frontend can read.

    `readError` takes `body.detail` as a plain sentence for every endpoint in
    this API, so `detail` must be a string and `session_status` its *sibling*.
    The regression this guards is `HTTPException(detail={...})`, which nests
    the dict under FastAPI's own key and yields `{"detail": {"detail": ...}}` —
    the frontend then renders an object where a sentence belongs.

    All three refusal codes in one test, because the bug is a property of how
    the body is built rather than of any single branch.
    """
    tid, uid, ids = agency
    monkeypatch.setattr(settings, "WA_SEND_DAILY_LIMIT", 50)

    # 409 — no usable session.
    _fake_client(monkeypatch, outcome=SendOutcome(ok=False, session_status="logged_out"))
    async with await _client_for(tid, uid) as http:
        conflict = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert conflict.status_code == 409
    body = conflict.json()
    assert isinstance(body["detail"], str)
    assert set(body) == {"detail", "session_status"}
    assert body["session_status"] == "logged_out"

    # 422 — no reachable number. No `session_status` at any depth.
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="X"),
    )
    async with await _client_for(tid, uid) as http:
        unprocessable = await http.post(SEND.format(cid=ids["no_phone"]), json=_body())
    assert unprocessable.status_code == 422
    body = unprocessable.json()
    assert isinstance(body["detail"], str)
    assert set(body) == {"detail"}

    # 429 — the daily cap. Flat, like the others, but carries `limit` and
    # `retry_after_seconds` (P5) — the shared shape both 429s use, agreed
    # with the frontend so it has one branch instead of two. No
    # `session_status`: neither 429 is a session problem.
    monkeypatch.setattr(settings, "WA_SEND_DAILY_LIMIT", 1)
    await _seed_sent_today(tid, uid, ids["with_phone"], 1)
    async with await _client_for(tid, uid) as http:
        capped = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert capped.status_code == 429
    body = capped.json()
    assert isinstance(body["detail"], str)
    assert set(body) == {"detail", "limit", "retry_after_seconds"}
    assert body["limit"] == "daily"
    assert isinstance(body["retry_after_seconds"], int)


async def test_a_refusal_after_dispatch_carries_its_activity_id_at_the_top_level(
    agency, monkeypatch
) -> None:
    """The one 409 that *does* write a row names it, still without nesting."""
    tid, uid, ids = agency
    _fake_client(monkeypatch, raises=GatewayRefusedError("not-authorized"))
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert resp.status_code == 409
    body = resp.json()
    assert isinstance(body["detail"], str)
    assert set(body) == {"detail", "session_status", "activity_id"}
    uuid.UUID(body["activity_id"])  # a real row was written, and named
    assert [row["status"] for row in await _activities(tid, ids["with_phone"])] == ["failed"]


async def test_agency_b_can_neither_send_to_nor_read_agency_as_candidate(
    agency, monkeypatch
) -> None:
    tid, uid, ids = agency
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="WAMSG-9"),
    )
    # A real, successful send by agency A, so there is an activity row for B to
    # fail to read.
    async with await _client_for(tid, uid) as http:
        assert (
            await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
        ).status_code == 200

    tid_b, uid_b = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid_b, "n": f"agency-{tid_b.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid_b, "t": tid_b, "e": f"u{uid_b.hex[:6]}@other.sg"},
        )
        await s.commit()
    try:
        async with await _client_for(tid_b, uid_b) as http:
            # 404, not 403: saying "exists, but not yours" is itself a
            # cross-tenant disclosure.
            send = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
            assert send.status_code == 404
            listing = await http.get(f"/api/candidates/{ids['with_phone']}/activities")
            assert listing.status_code == 404
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tid_b})
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid_b})
            await s.commit()

    # And agency A's own row is untouched by B's attempt.
    rows = await _activities(tid, ids["with_phone"])
    assert [row["status"] for row in rows] == ["sent"]


async def _seed_wa_session(tid, uid, *, next_send_allowed_at=None) -> None:
    """A `wa_sessions` row for this recruiter — P5's pre-dispatch claim locks
    this row (`SELECT ... FOR UPDATE`), so a spacing or cap-race test needs
    one to exist, exactly as a real paired recruiter would have."""
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO wa_sessions (id, tenant_id, user_id, status, "
                "next_send_allowed_at) VALUES (:i, :t, :u, 'connected', :n)"
            ),
            {"i": uuid.uuid4(), "t": tid, "u": uid, "n": next_send_allowed_at},
        )
        await s.commit()


async def test_two_concurrent_different_messages_the_cap_admits_exactly_one(
    agency, monkeypatch
) -> None:
    """The P4-review race: two *different* messages, not a replay of one key,
    both racing the daily cap. Only the pre-dispatch row lock on `wa_sessions`
    (P5) closes this — the unique index on `client_request_id` does nothing
    here, because the two requests carry different keys."""
    tid, uid, ids = agency
    await _seed_wa_session(tid, uid)
    monkeypatch.setattr(settings, "WA_SEND_DAILY_LIMIT", 1)
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="RACE"),
    )

    async def _send():
        async with await _client_for(tid, uid) as http:
            return await http.post(SEND.format(cid=ids["with_phone"]), json=_body())

    first, second = await asyncio.gather(_send(), _send())
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 429], "exactly one send got through the cap of 1"
    rows = await _activities(tid, ids["with_phone"])
    assert len([r for r in rows if r["status"] == "sent"]) == 1


async def test_a_pending_row_counts_toward_the_daily_cap(agency, monkeypatch) -> None:
    """A send still in flight is a send that happened, for the purpose of the
    cap — not counting it is exactly the hole `pending` exists to close."""
    tid, uid, ids = agency
    await _seed_wa_session(tid, uid)
    monkeypatch.setattr(settings, "WA_SEND_DAILY_LIMIT", 1)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_activities (id, tenant_id, candidate_id, user_id, "
                "activity_type, channel, status, client_request_id, created_at, updated_at) "
                "VALUES (:i, :t, :c, :u, 'whatsapp_sent', 'whatsapp', 'pending', :k, now(), now())"
            ),
            {"i": uuid.uuid4(), "t": tid, "c": ids["with_phone"], "u": uid, "k": uuid.uuid4()},
        )
        await s.commit()

    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="X"),
    )
    async with await _client_for(tid, uid) as http:
        resp = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert resp.status_code == 429
    assert resp.json()["limit"] == "daily"


async def test_a_retry_at_exactly_retry_after_seconds_succeeds(agency, monkeypatch) -> None:
    """The jitter is drawn once, at admission, and never re-rolled on a
    refusal (P5 review): a caller who waits exactly the quoted
    `retry_after_seconds` and retries must get through."""
    tid, uid, ids = agency
    deadline = datetime.now(UTC) + timedelta(seconds=1)
    await _seed_wa_session(tid, uid, next_send_allowed_at=deadline)
    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="X"),
    )

    async with await _client_for(tid, uid) as http:
        refused = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert refused.status_code == 429
    body = refused.json()
    assert body["limit"] == "interval"
    retry_after = body["retry_after_seconds"]
    assert refused.headers["retry-after"] == str(retry_after)

    await asyncio.sleep(retry_after)

    async with await _client_for(tid, uid) as http:
        retried = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert retried.status_code == 200
    assert retried.json()["status"] == "sent"


async def _next_send_allowed_at(tid, uid):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT next_send_allowed_at FROM wa_sessions "
                    "WHERE tenant_id = :t AND user_id = :u"
                ),
                {"t": tid, "u": uid},
            )
        ).scalar_one()


async def test_a_discarded_claim_gives_back_the_deadline_it_reserved(
    agency, monkeypatch
) -> None:
    """The bug: `_claim_send` reserves the next slot the instant a send is
    admitted, but a send that never reached the gateway (unreachable here)
    still charged the recruiter a full interval before this fix. An
    immediate retry must be admitted, not 429'd for a send that never
    happened."""
    tid, uid, ids = agency
    await _seed_wa_session(tid, uid, next_send_allowed_at=None)
    _fake_client(monkeypatch, raises=GatewayUnreachableError("unreachable"))

    async with await _client_for(tid, uid) as http:
        first = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert first.status_code == 409
    assert first.json()["session_status"] == "gateway_unreachable"

    # Nothing was dispatched, so the deadline the discarded claim reserved
    # must be given back rather than left standing.
    assert await _next_send_allowed_at(tid, uid) is None

    _fake_client(
        monkeypatch,
        outcome=SendOutcome(ok=True, session_status="connected", provider_message_id="X"),
    )
    async with await _client_for(tid, uid) as http:
        retried = await http.post(SEND.format(cid=ids["with_phone"]), json=_body())
    assert retried.status_code == 200, (
        "an immediate retry must not be 429'd for a send that never happened"
    )
    assert retried.json()["status"] == "sent"


async def test_a_discard_does_not_roll_back_a_later_sends_deadline(
    agency, monkeypatch
) -> None:
    """If a second, later send has already moved `next_send_allowed_at`
    forward by the time an earlier claim is discarded, that later send owns
    the deadline — the discard's compare-and-set must not touch it."""
    tid, uid, ids = agency
    await _seed_wa_session(tid, uid, next_send_allowed_at=None)

    # First claim: dispatch never happens (simulated directly, since the
    # route always dispatches inline) — reserve, then discard, but a second,
    # later claim has since moved the deadline forward before the discard
    # runs.
    activity, claimed, deadline_claim = await wa_api._claim_send(
        tenant_uuid=tid,
        user_uuid=uid,
        candidate_id=ids["with_phone"],
        client_request_id=uuid.uuid4(),
        message="first",
    )
    assert claimed and deadline_claim is not None

    later_deadline = datetime.now(UTC) + timedelta(hours=1)
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE wa_sessions SET next_send_allowed_at = :n "
                "WHERE tenant_id = :t AND user_id = :u"
            ),
            {"n": later_deadline, "t": tid, "u": uid},
        )
        await s.commit()

    await wa_api._discard_claim(tid, activity.id, uid, deadline_claim)

    stored = await _next_send_allowed_at(tid, uid)
    assert stored == later_deadline, (
        "a later send's deadline must survive an earlier claim's discard"
    )
