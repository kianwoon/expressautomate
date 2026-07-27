"""arq entrypoint: `uv run arq app.workers.settings.WorkerSettings`.

Deliberately a third module. `jobs` imports `enqueue` from `queue`, so a
registry living in either of them would make the two mutually dependent, and
whichever was imported first would fail on a partially initialised module.
Everything that imports both sits here, above both.

This is the job-processing process. The periodic recovery tasks run in
`app.workers.main` instead — separate lifecycles, separate failure modes: a
wedged arq worker still gets fresh work queued, and a crashed supervisor does
not stop work already in the queue.
"""

from app.core.config import settings
from app.core.logging import configure_logging
from app.services.graph.client import warn_if_unconfigured
from app.workers.jobs import (
    backfill_mailbox_job,
    classify_batch,
    classify_email,
    delta_sync_mailbox,
    fetch_email,
    reauthorize_subscription,
    recreate_subscription,
)
from app.workers.queue import redis_settings


async def _announce(ctx: dict) -> None:
    """arq's startup hook. Takes the context it passes and ignores it.

    `configure_logging` belongs here because nothing else runs it in this
    process: arq is launched against this class directly, so unlike `api` and
    `worker` there is no entrypoint of ours to do it. Without it every line
    this process emits — including the warning below — renders with structlog's
    console defaults instead of the JSON the log pipeline expects, which is
    precisely the sort of difference that gets a real error skimmed past.
    """
    configure_logging()
    warn_if_unconfigured("arq")


class WorkerSettings:
    # Note: `redis_settings()` runs when this class body is evaluated, so
    # importing this module needs a valid REDIS_URL. That is right for the
    # worker process — it cannot run without Redis — but it means anything
    # merely inspecting the registry needs one too, which is why CI sets a
    # local DSN it never connects to.
    # Every name any producer enqueues must appear here. A missing one is an
    # error inside arq, on the far side of the queue, where the producer sees
    # success and nothing surfaces.
    #
    # `classify_batch` is the normal path — the `classify_fetched` sweep
    # enqueues it, one job per batch. `classify_email` stays because
    # `rescan_stuck` still names it: a row stranded at `fetched` or
    # `classifying` is recovered one email at a time, deliberately, since a
    # batch that died may have died because of one of its members.
    #
    # `extract_email` is still absent: it belongs to the rest of the extraction
    # plan. Until it lands, classified rows accumulate and `rescan_stuck`
    # retries them — visible in the logs rather than lost.
    functions = [
        fetch_email,
        classify_batch,
        classify_email,
        backfill_mailbox_job,
        delta_sync_mailbox,
        recreate_subscription,
        reauthorize_subscription,
    ]
    # Every function above but the two classification jobs ends in a Graph call. Said once
    # here rather than discovered one failed job at a time.
    on_startup = staticmethod(_announce)
    redis_settings = redis_settings()
    poll_delay = settings.ARQ_POLL_DELAY_SECONDS
    max_jobs = settings.ARQ_MAX_JOBS
    max_tries = settings.ARQ_MAX_TRIES
