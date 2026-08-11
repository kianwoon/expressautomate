"""arq queue plumbing (plan §7).

Two decisions shape this module.

**Enqueue fails soft.** Redis cannot join the Postgres transaction that just
committed a row, so there is an unavoidable gap between "the email is durably
recorded" and "a job exists to process it". Raising into the webhook would turn
that gap into a 500 on a request whose durable work is already done — Graph
would retry a notification whose row already exists, and the user would still
see nothing. So `enqueue` reports failure and leaves the row for
`rescan_stuck`, which exists precisely to close this gap.

**The pool is opened once.** Upstash bills per command and caps concurrent
connections, and the webhook has roughly three seconds to answer. Opening a
pool per notification would add a TLS handshake to that budget and burn a
connection slot each time.

This module imports nothing from `app.workers.jobs`. The jobs import `enqueue`
from here, so the arq registry that imports both lives in a third module
(`app.workers.settings`) — otherwise importing either one first would fail on a
partially initialised module.
"""

import asyncio

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_pool: ArqRedis | None = None
# Guards the open, so a burst of notifications on a cold process opens one pool
# rather than one per concurrent request.
_pool_lock = asyncio.Lock()


def redis_settings() -> RedisSettings:
    """Connection settings from the configured DSN.

    `from_dsn` honours the `rediss://` scheme, which matters: the DSN carries a
    password, and downgrading it to plaintext would put that credential on the
    wire.
    """
    return RedisSettings.from_dsn(settings.REDIS_URL)


async def _create_pool() -> ArqRedis:
    """Seam for tests, which must never open a real connection."""
    return await create_pool(redis_settings())


async def redis_pool() -> ArqRedis:
    """The shared pool, opened on first use.

    A failed open is deliberately not cached — a Redis blip during startup must
    not wedge the queue for the life of the process.
    """
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            _pool = await _create_pool()
    return _pool


def reset_pool() -> None:
    """Drop the cached pool. Used by tests; harmless elsewhere."""
    global _pool
    _pool = None


async def close_pool() -> None:
    """Release the pool on shutdown, so the process exits without warnings."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue(name: str, *, queue_name: str | None = None, **kwargs) -> bool:
    """Enqueue a job. Returns whether it was accepted.

    `queue_name` routes the job to a specific arq queue. The interactive
    analysis jobs (job/candidate intelligence) use it so a user's click is
    consumed by the dedicated interactive worker with its own slot budget —
    never starved behind a background replay/extraction backlog on the default
    queue. `None` (the default) enqueues to arq's default queue, so every
    existing caller is unchanged.

    Never raises. The caller has already committed something durable, and the
    recovery sweep is what turns a lost job back into a queued one. Failing
    soft is not failing silently, though — the log line is the only signal that
    `rescan_stuck` is doing work it should not have to.
    """
    try:
        pool = await redis_pool()
        # `_queue_name` only travels when set, so the default path's wire call
        # is byte-identical to the pre-queue_name era.
        job_kwargs = dict(kwargs)
        if queue_name is not None:
            job_kwargs["_queue_name"] = queue_name
        job = await pool.enqueue_job(name, **job_kwargs)
    except Exception:
        log.exception("enqueue_failed", job=name)
        return False

    if job is None:
        # arq returns None when the job id already exists. It cannot happen
        # today — no caller passes `_job_id`, so arq generates a uuid — but
        # returning True here would report a job that does not exist, and the
        # row would then wait for `rescan_stuck` while the caller believed it
        # was queued.
        log.warning("enqueue_deduplicated", job=name)
        return False
    return True
