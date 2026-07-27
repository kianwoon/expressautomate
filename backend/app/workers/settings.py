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
from app.workers.jobs import fetch_email
from app.workers.queue import redis_settings


class WorkerSettings:
    functions = [fetch_email]
    redis_settings = redis_settings()
    poll_delay = settings.ARQ_POLL_DELAY_SECONDS
    max_jobs = settings.ARQ_MAX_JOBS
    max_tries = settings.ARQ_MAX_TRIES
