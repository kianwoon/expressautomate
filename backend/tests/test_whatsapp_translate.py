"""Translation for the WhatsApp draft — service and `POST /whatsapp-translate`.

Two layers under test:

- `translate_message` talks to Google Translate's free `translate_a/single`
  endpoint. The network is replaced by a fake httpx transport, so the test
  asserts on how the nested response shape is parsed (segments joined at index
  0) and on how failures surface — never on whether Google is up.
- The route applies session scope and input validation, then turns a
  `TranslateError` into a 502 rather than letting it through as a 500. The
  honesty rule is the same one the send path follows: a failure the recruiter
  cannot act on is reported in words they can, and never as a silent English
  fallback a candidate would then read.
"""

import json
import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

import app.api.candidate_whatsapp as wa_api
from app.main import app
from app.services.whatsapp_translate import (
    TranslateError,
    translate_message,
)
from tests.conftest import AdminSessionLocal, sign_in

TRANSLATE = "/api/candidates/{cid}/whatsapp-translate"
ENDPOINT = "https://translate.google.com/translate_a/single"


class _FakeTransport(httpx.AsyncBaseTransport):
    """Return a canned JSON body for the one request the service makes."""

    def __init__(self, payload, *, status_code: int = 200):
        self._payload = payload
        self._status = status_code
        self.request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(
            self._status,
            content=json.dumps(self._payload).encode(),
            headers={"content-type": "application/json; charset=UTF-8"},
            request=request,
        )


class _ErrorTransport(httpx.AsyncBaseTransport):
    """Raise a transport-level error to exercise the httpx-error path."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise self._exc


# --- service -----------------------------------------------------------------


async def test_translates_by_joining_segment_translations() -> None:
    # The real endpoint returns one segment per sentence: each is
    # `[translation, original, ...]`. Joining index 0 of every segment rebuilds
    # the full message, including the line breaks the draft put between
    # paragraphs (a paragraph break is just another translated segment).
    payload = [
        [["你好", "Hi", None, None, 10], ["这是林。", "This is Lim.", None, None, 9]],
        None,
        "en",
        None,
        [],
    ]
    transport = _FakeTransport(payload)
    out = await translate_message("Hi\n\nThis is Lim.", "chinese", transport=transport)
    assert out == "你好这是林。"

    # The language code is sent as `tl`, the source fixed at English.
    request = transport.request
    assert request is not None
    assert request.url.params["tl"] == "zh-CN"
    assert request.url.params["sl"] == "en"
    assert request.url.params["client"] == "gtx"
    assert "Hi" in request.url.params["q"]


async def test_tamil_and_malay_map_to_their_codes() -> None:
    for lang, code in (("tamil", "ta"), ("malay", "ms")):
        transport = _FakeTransport([[["x", "Hi", None, None, 1]], None, "en"])
        await translate_message("Hi", lang, transport=transport)
        assert transport.request.url.params["tl"] == code


async def test_an_unknown_language_is_rejected_before_any_request() -> None:
    # The caller validates, so reaching the service with a bad key is a
    # programming error — raise, never silently no-op.
    with pytest.raises(TranslateError):
        await translate_message("Hi", "klingon", transport=_FakeTransport([[["x", "Hi"]]]))


async def test_a_transport_error_becomes_translate_error() -> None:
    with pytest.raises(TranslateError):
        await translate_message(
            "Hi", "chinese", transport=_ErrorTransport(httpx.ConnectError("boom"))
        )


async def test_a_non_200_becomes_translate_error() -> None:
    with pytest.raises(TranslateError):
        await translate_message(
            "Hi", "chinese", transport=_FakeTransport([[]], status_code=403)
        )


async def test_an_unexpected_body_shape_becomes_translate_error() -> None:
    # `body[0]` missing or empty — Google sometimes emits trailing `null`s; a
    # response with no segments is unusable, not empty.
    with pytest.raises(TranslateError):
        await translate_message("Hi", "chinese", transport=_FakeTransport([None]))


# --- route -------------------------------------------------------------------


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
                "INSERT INTO candidates (id, tenant_id, full_name, phone_e164, "
                "current_title) VALUES (:i, :t, :n, :p, 'Engineer')"
            ),
            {"i": cid, "t": tid, "n": "Hui Ling Tan", "p": "+6582217734"},
        )
        await s.commit()
    yield tid, uid, cid
    async with AdminSessionLocal() as s:
        await s.execute(text("DELETE FROM candidate_activities WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM candidates WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


def _payload(source: str, lang: str) -> dict:
    return {"source_text": source, "target_language": lang}


async def test_translate_requires_a_session() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.post(TRANSLATE.format(cid=uuid.uuid4()), json=_payload("Hi", "chinese"))
    assert resp.status_code == 401


async def test_english_is_a_no_op_that_never_calls_google(agency, monkeypatch) -> None:
    tid, uid, cid = agency

    async def fail(*a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("english must not call the translation service")

    monkeypatch.setattr(wa_api, "translate_message", fail)
    async with await _client_for(tid, uid) as http:
        resp = await http.post(TRANSLATE.format(cid=cid), json=_payload("Hello there", "english"))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"translation": "Hello there"}


async def test_a_valid_language_returns_the_translation(agency, monkeypatch) -> None:
    tid, uid, cid = agency

    async def fake_translate(source_text, target_language, **_):  # noqa: ANN001
        assert source_text == "Hello there"
        assert target_language == "chinese"
        return "你好"

    monkeypatch.setattr(wa_api, "translate_message", fake_translate)
    async with await _client_for(tid, uid) as http:
        resp = await http.post(TRANSLATE.format(cid=cid), json=_payload("Hello there", "chinese"))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"translation": "你好"}


async def test_an_unknown_language_is_422(agency) -> None:
    tid, uid, cid = agency
    async with await _client_for(tid, uid) as http:
        resp = await http.post(TRANSLATE.format(cid=cid), json=_payload("Hi", "klingon"))
    assert resp.status_code == 422


async def test_a_translate_failure_is_502_not_500(agency, monkeypatch) -> None:
    tid, uid, cid = agency

    async def fail(*a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise TranslateError("nope")

    monkeypatch.setattr(wa_api, "translate_message", fail)
    async with await _client_for(tid, uid) as http:
        resp = await http.post(TRANSLATE.format(cid=cid), json=_payload("Hi", "chinese"))
    assert resp.status_code == 502
    assert "detail" in resp.json()
