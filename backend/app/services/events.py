"""Cross-process nudges to a connected dashboard (Redis pub/sub).

The dashboard has to learn that something changed without asking. Polling was
rejected — every recruiter's browser would spend the day asking a question whose
answer is almost always "nothing" — and the backend already knows the exact
moment a row lands, because Graph tells it.

**Why Redis and not Postgres LISTEN/NOTIFY.** Three Koyeb services share this
codebase (`api`, `worker`, `arq`), and the process that writes the row is never
the process holding the browser's connection: mail is recorded by `api`'s
webhook or by `worker`'s sweeps, extractions by `arq`, and only `api` has an
open `/api/events` stream. A signal that cannot cross a process boundary would
therefore fire into the void most of the time. LISTEN/NOTIFY could cross it,
but it would pin one pooled database connection per connected browser for the
life of the stream — the pool is sized for short requests, and a dozen open
dashboards would starve it.

**Publishing must not be able to break ingestion.** This is the single most
important property here. A nudge is a convenience; the mail is the product. So
`publish` swallows everything, including a Redis that is unreachable, missing
from the environment, or slow. The worst outcome allowed is a dashboard that
needs a manual reload — never an email that was not stored because the
refresh mechanism was down.

**One client per process.** Upstash bills per command and caps concurrent
connections, so a client is opened once and shared. The arq pool in
`app.workers.queue` is deliberately not reused: it is an `ArqRedis` whose
lifecycle belongs to the queue, and a long-lived `pubsub()` conversation on it
would sit on a connection the queue expects to be able to use. Each subscriber
does take its own connection out of this client's pool — that is inherent to
pub/sub, since a subscribed connection can do nothing else.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# The wire protocol between this backend and the dashboard, not configuration:
# both halves have to agree on these three strings or the feature silently does
# nothing, and no deployment would ever want to change them. Hence literals
# here rather than settings, named once so no caller spells one wrong.
KIND_MAIL = "mail"
KIND_EXTRACTION = "extraction"
KIND_MAILBOX = "mailbox"
EVENT_KINDS = frozenset({KIND_MAIL, KIND_EXTRACTION, KIND_MAILBOX})

_client: Redis | None = None
# Guards the open, so a burst of notifications on a cold process opens one
# client rather than one per concurrent request.
_client_lock = asyncio.Lock()


def channel_for(tenant_id: uuid.UUID) -> str:
    """The channel one tenant's events travel on.

    A channel per tenant, rather than one channel filtered on receipt: the
    filtering would then live in application code that could be got wrong,
    and a bug there is a cross-agency leak (§18). Redis will not deliver a
    message to a subscriber that never asked for the channel.
    """
    return f"{settings.EVENTS_CHANNEL_PREFIX}{tenant_id}"


def _create_client() -> Redis:
    """Seam for tests, which must never open a real connection."""
    # `decode_responses` so a subscriber receives `str` rather than `bytes`;
    # the payload is JSON either way, but decoding at the edge keeps every
    # caller from having to remember which it got.
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


async def client() -> Redis | None:
    """The shared client, opened on first use. None when unconfigured.

    Returning None rather than raising is what lets a deployment with no
    `REDIS_URL` — a local dev run, a test process — behave as though nothing
    is listening, which is exactly true.

    A failed open is deliberately not cached, so a Redis blip during startup
    does not wedge events for the life of the process.
    """
    global _client
    if _client is not None:
        return _client
    if not settings.REDIS_URL:
        return None
    async with _client_lock:
        if _client is None:
            _client = _create_client()
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests; harmless elsewhere."""
    global _client
    _client = None


async def close_client() -> None:
    """Release the client on shutdown, so the process exits without warnings."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def publish(tenant_id: uuid.UUID, kind: str) -> bool:
    """Nudge one tenant's open dashboards. Returns whether it went out.

    Never raises — see the module docstring. Callers are on ingestion paths
    that have already committed something durable, and a failure here must
    cost them nothing.

    Call this **after** the transaction commits. A nudge that overtakes its own
    row makes the browser refetch and see nothing, which reads as a broken
    feature — worse than no nudge, because the user then distrusts the ones
    that do work.

    The payload carries no business data on purpose: a `kind` and nothing else.
    The browser refetches through the existing RLS-scoped endpoints, so the
    database stays the only thing that decides what a given session may see.
    """
    if kind not in EVENT_KINDS:
        # A typo'd kind is a programming error, and a silent no-op would hide
        # it until someone wondered why the dashboard never woke up. Loud in
        # the log, still harmless to the caller.
        log.error("event_kind_unknown", kind=kind)
        return False
    try:
        conn = await client()
        if conn is None:
            return False
        await conn.publish(channel_for(tenant_id), json.dumps({"kind": kind}))
    except Exception:
        # Includes a dead Redis. The row is already committed; the only loss is
        # that a dashboard will need a reload. This log line is the sole signal
        # that live updates have stopped working.
        log.warning("event_publish_failed", kind=kind, exc_info=True)
        return False
    return True


async def subscribe(tenant_id: uuid.UUID) -> AsyncGenerator[str | None, None]:
    """Yield this tenant's event payloads until the consumer stops asking.

    Yields `None` when nothing arrived within `EVENTS_HEARTBEAT_SECONDS`, which
    is how the SSE endpoint knows to emit a keep-alive without racing a second
    timer against this one. The consumer therefore gets a tick on a predictable
    cadence whether or not there is news.

    Unconfigured Redis yields only those ticks. A dashboard then behaves as it
    did before this feature existed rather than failing to load at all.

    The subscription is torn down in `finally`, so a browser that closes its
    connection releases both the Redis subscription and the connection under
    it. Leaking one per disconnect would exhaust the client's pool after a day
    of ordinary page navigation.
    """
    interval = settings.EVENTS_HEARTBEAT_SECONDS
    conn = await client()
    if conn is None:
        while True:
            await asyncio.sleep(interval)
            yield None

    channel = channel_for(tenant_id)
    pubsub = conn.pubsub()
    try:
        await pubsub.subscribe(channel)
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=interval
            )
            yield message["data"] if message else None
    finally:
        try:
            await pubsub.aclose()
        except Exception:
            # Closing a subscription whose connection has already gone is not
            # worth failing a response that has otherwise finished cleanly.
            log.debug("event_subscription_close_failed", exc_info=True)
