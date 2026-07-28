"""Dashboard live updates: the SSE endpoint and its pub/sub layer.

Two properties are worth a test far more than the plumbing is.

**A nudge must not cross tenants.** The channel name is derived server-side
from the session cookie, so the only way one agency could learn that another
had mail would be a bug in that derivation — and it would be silent, because a
nudge carries no data to notice in a log. So it is asserted directly, against
the same code the endpoint uses.

**Publishing must never break ingestion.** A dead Redis is a stale dashboard,
never a lost email. The publisher swallows everything, and this is where that
promise is held to.

Nothing here talks to Redis; the client is always faked, exactly as
`tests/test_queue.py` fakes the arq pool.
"""

import asyncio
import json
import uuid

import pytest
import structlog.testing
from fastapi.testclient import TestClient

from app.main import app
from app.services import events


@pytest.fixture(autouse=True)
def _reset_client():
    """The client is cached across calls; each test starts from a clean one."""
    events.reset_client()
    yield
    events.reset_client()


class _FakePubSub:
    """One subscriber's view of `_FakeRedis`, closely enough for these tests.

    `get_message` returns None on timeout rather than blocking, which is how the
    real client signals "nothing arrived" and how the endpoint knows to send a
    heartbeat instead.
    """

    def __init__(self, broker: "_FakeRedis") -> None:
        self._broker = broker
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.channels: set[str] = set()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.channels.add(channel)
        self._broker.subscribers.setdefault(channel, []).append(self)

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout=None):
        try:
            data = await asyncio.wait_for(self._queue.get(), timeout=0.05)
        except TimeoutError:
            return None
        return {"type": "message", "data": data}

    async def aclose(self) -> None:
        self.closed = True
        for channel in self.channels:
            self._broker.subscribers.get(channel, []).remove(self)


class _FakeRedis:
    def __init__(self) -> None:
        self.subscribers: dict[str, list[_FakePubSub]] = {}
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        for subscriber in self.subscribers.get(channel, []):
            subscriber._queue.put_nowait(payload)
        return len(self.subscribers.get(channel, []))

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)

    async def aclose(self) -> None:
        return None


def _use(fake: _FakeRedis, monkeypatch) -> None:
    monkeypatch.setattr(events, "_create_client", lambda: fake)
    # `client()` returns None when REDIS_URL is blank, which is right in
    # production and useless here — the fake would never be reached.
    monkeypatch.setattr(events.settings, "REDIS_URL", "redis://localhost:6379")


async def test_a_nudge_reaches_a_subscriber_on_the_same_tenant(monkeypatch):
    fake = _FakeRedis()
    _use(fake, monkeypatch)
    tenant_id = uuid.uuid4()

    stream = events.subscribe(tenant_id)
    # Drives the generator as far as its own subscribe call, so the publish
    # below has somebody to reach. Published first, the message would be sent
    # to an empty channel and dropped — which is Redis pub/sub working, not a
    # bug, and would make this test flaky rather than red.
    assert await anext(stream) is None

    assert await events.publish(tenant_id, events.KIND_MAIL) is True
    payload = None
    for _ in range(20):
        payload = await anext(stream)
        if payload is not None:
            break
    await stream.aclose()

    assert json.loads(payload) == {"kind": events.KIND_MAIL}


async def test_a_nudge_for_one_tenant_never_reaches_another(monkeypatch):
    """The isolation guarantee (§18), asserted on the derivation itself.

    Agency A having mail is information about Agency A. A nudge carries no rows,
    but it does say "something just happened here", and a dashboard that acted
    on someone else's would refetch — reading nothing, thanks to RLS, but
    revealing the other agency's activity in its own timing.
    """
    fake = _FakeRedis()
    _use(fake, monkeypatch)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    stream_b = events.subscribe(tenant_b)
    assert await anext(stream_b) is None

    assert await events.publish(tenant_a, events.KIND_EXTRACTION) is True

    delivered = None
    for _ in range(10):
        delivered = await anext(stream_b)
        if delivered is not None:
            break
    await stream_b.aclose()

    assert delivered is None, "a tenant received another tenant's event"
    assert events.channel_for(tenant_a) != events.channel_for(tenant_b)


async def test_a_dead_redis_does_not_break_the_caller(monkeypatch):
    """Ingestion has already committed a row. A nudge may not undo that.

    This is the whole reason `publish` swallows exceptions: the caller is on the
    ingestion path, and an email lost because the refresh mechanism was down
    would be the worst possible trade for a feature that only saves a reload.
    """

    def unreachable():
        raise ConnectionError("no redis")

    monkeypatch.setattr(events, "_create_client", unreachable)
    monkeypatch.setattr(events.settings, "REDIS_URL", "redis://localhost:6379")

    with structlog.testing.capture_logs() as logs:
        assert await events.publish(uuid.uuid4(), events.KIND_MAIL) is False
    assert any(entry["event"] == "event_publish_failed" for entry in logs)


async def test_a_slow_publish_does_not_break_the_caller(monkeypatch):
    """A reachable-but-broken Redis fails on the command, not the connect."""

    class _Broken(_FakeRedis):
        async def publish(self, channel: str, payload: str) -> int:
            raise ConnectionResetError("connection dropped mid-command")

    _use(_Broken(), monkeypatch)

    assert await events.publish(uuid.uuid4(), events.KIND_MAILBOX) is False


async def test_an_unconfigured_redis_is_not_an_error(monkeypatch):
    """No REDIS_URL is a deployment without live updates, not a broken one.

    A local dev run has no Redis, and the dashboard must still load and behave
    as it did before this feature existed.
    """
    monkeypatch.setattr(events.settings, "REDIS_URL", "")
    assert await events.client() is None
    assert await events.publish(uuid.uuid4(), events.KIND_MAIL) is False


async def test_an_unknown_kind_is_refused(monkeypatch):
    """The three kinds are a protocol with the browser, not free text.

    A typo would otherwise publish a frame the dashboard ignores, and the
    feature would look broken with nothing in the log to say why.
    """
    fake = _FakeRedis()
    _use(fake, monkeypatch)

    with structlog.testing.capture_logs() as logs:
        assert await events.publish(uuid.uuid4(), "opportunity") is False
    assert any(entry["event"] == "event_kind_unknown" for entry in logs)
    assert fake.published == []


def test_the_endpoint_refuses_an_unsigned_request():
    """No cookie, no stream.

    A 401 rather than a stream carrying an error, deliberately: EventSource
    stops retrying on a non-2xx and would reconnect forever against a 200.
    """
    response = TestClient(app).get("/api/events")

    assert response.status_code == 401
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_the_endpoint_lives_under_the_api_prefix():
    """Anything outside /api is shadowed by the static site mount."""
    assert "/api/events" in app.openapi()["paths"]


def test_the_stream_authenticates_before_it_streams(monkeypatch):
    """A forged cookie must not reach the subscribe call.

    Asserted by making a subscription fatal: if auth were checked lazily inside
    the generator, the response would already be a 200 stream by the time this
    raised, and the browser would retry against it forever.
    """

    def unreachable() -> None:
        raise AssertionError("subscribed before authenticating")

    monkeypatch.setattr(events, "_create_client", unreachable)

    client = TestClient(app)
    client.cookies.set("ea_session", "not-a-signed-value")
    assert client.get("/api/events").status_code == 401
