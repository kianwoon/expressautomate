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
from app.workers.jobs import (
    delta_sync_mailbox,
    fetch_email,
    reauthorize_subscription,
    recreate_subscription,
)
from app.workers.queue import redis_settings


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
    # `classify_email` and `extract_email` are deliberately absent: they belong
    # to the extraction plan. Until it lands, fetched rows accumulate and
    # `rescan_stuck` retries them — visible in the logs rather than lost.
    functions = [
        fetch_email,
        delta_sync_mailbox,
        recreate_subscription,
        reauthorize_subscription,
    ]
    redis_settings = redis_settings()
    poll_delay = settings.ARQ_POLL_DELAY_SECONDS
    max_jobs = settings.ARQ_MAX_JOBS
    max_tries = settings.ARQ_MAX_TRIES
