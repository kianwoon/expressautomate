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

from arq.worker import func

from app.core.config import settings
from app.core.logging import configure_logging
from app.services.graph.client import warn_if_unconfigured
from app.workers.candidate_intelligence_jobs import run_candidate_intelligence
from app.workers.cv_jobs import parse_candidate_cv
from app.workers.delivery_jobs import deliver_notification
from app.workers.discovery_jobs import run_client_discovery
from app.workers.embedding_jobs import compute_candidate_embedding
from app.workers.import_jobs import run_candidate_import
from app.workers.ingest_jobs import ingest_candidate_cv
from app.workers.job_intelligence_jobs import run_job_intelligence
from app.workers.jobs import (
    backfill_mailbox_job,
    classify_batch,
    classify_email,
    delta_sync_mailbox,
    extract_email,
    fetch_email,
    reauthorize_subscription,
    recreate_subscription,
    replay_email,
)
from app.workers.queue import redis_settings
from app.workers.sourcing_jobs import run_sourcing


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


# The two user-initiated analysis jobs, wrapped once and shared between the
# default worker registry (which keeps them for the historical single-queue
# shape) and the interactive worker registry (which runs only these two on the
# interactive queue). Defining them here rather than inline in each list means
# the timeout a job runs under cannot drift between the two registries.
_JOB_INTELLIGENCE_FUNC = func(
    run_job_intelligence,
    name="run_job_intelligence",
    timeout=settings.JOB_INTEL_JOB_TIMEOUT_SECONDS,
)
_CANDIDATE_INTELLIGENCE_FUNC = func(
    run_candidate_intelligence,
    name="run_candidate_intelligence",
    timeout=settings.CANDIDATE_INTELLIGENCE_JOB_TIMEOUT_SECONDS,
)


class InteractiveWorkerSettings:
    """The dedicated worker for the interactive analysis queue.

    Consumes only `ARQ_INTERACTIVE_QUEUE`, and only the two analysis jobs land
    there. Its own `max_jobs` budget is the point: a replay/extraction backlog
    on the default queue occupies the default worker's slots, and this worker
    keeps free slots for a recruiter's click no matter how deep that backlog
    gets. `app.workers.run_arq` runs both workers in one process.

    `redis_settings()` runs when this class body is evaluated, exactly as it
    does for `WorkerSettings` — both workers in the same process share one
    Redis and one event loop, but each still needs its own pool connection.
    """

    functions = [_JOB_INTELLIGENCE_FUNC, _CANDIDATE_INTELLIGENCE_FUNC]
    queue_name = settings.ARQ_INTERACTIVE_QUEUE
    redis_settings = redis_settings()
    poll_delay = settings.ARQ_POLL_DELAY_SECONDS
    max_jobs = settings.ARQ_INTERACTIVE_MAX_JOBS
    max_tries = settings.ARQ_MAX_TRIES
    on_startup = staticmethod(_announce)


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
    # `extract_email` is named by two producers — the classification jobs on a
    # `recruitment` verdict, and `rescan_stuck` for rows stalled at `classified`
    # or `extracting`. Until it was registered here those enqueues succeeded and
    # then errored inside arq, on the far side of the queue, leaving four real
    # recruitment emails parked at `classified` and a dashboard reporting no
    # job orders at all.
    functions = [
        fetch_email,
        classify_batch,
        classify_email,
        extract_email,
        replay_email,
        backfill_mailbox_job,
        delta_sync_mailbox,
        recreate_subscription,
        reauthorize_subscription,
        # Notifications. Enqueued by `emit_and_enqueue` after the opportunity
        # commits, and by `flush_notifications` for rows whose enqueue was lost.
        deliver_notification,
        # The two jobs here with a timeout of their own. Everything above is
        # bounded by the service it calls — Graph and the model client both
        # carry their own — but a CV parse runs a document parser on bytes a
        # stranger uploaded, and a single-page FlateDecode bomb inflates
        # inside `pypdf` where nothing of ours can interrupt it. `name` is
        # given explicitly: producers enqueue the string "parse_candidate_cv",
        # and a wrapper registered under any other name would fail on the far
        # side of the queue, where the producer already saw success.
        func(
            parse_candidate_cv,
            name="parse_candidate_cv",
            timeout=settings.CV_PARSE_TIMEOUT_SECONDS,
        ),
        # The ingest front half: a CV uploaded with no candidate named is read,
        # its identity extracted, and a candidate matched or created before the
        # roles/skills parse runs. Same timeout ceiling as the parse — a
        # FlateDecode bomb inflates inside `pypdf` here too — and the same
        # explicit `name`, because producers enqueue the string
        # "ingest_candidate_cv" and a wrapper under any other name fails on the
        # far side of the queue. `rescan_stuck` routes `ingest_pending` and
        # `ingesting` rows to this name; a mismatch would strand the document.
        func(
            ingest_candidate_cv,
            name="ingest_candidate_cv",
            timeout=settings.CV_INGEST_TIMEOUT_SECONDS,
        ),
        # An import is database work rather than a model call, but it is
        # database work whose size the uploader chooses: five hundred rows,
        # each one a match query and a write. The timeout is what keeps one
        # oversized file from holding a worker slot indefinitely, and a run it
        # cuts short is left at `parsing` for `rescan_stuck` to re-enqueue.
        # `name` is given explicitly for the same reason as above: producers
        # enqueue the string "run_candidate_import".
        func(
            run_candidate_import,
            name="run_candidate_import",
            timeout=settings.IMPORT_JOB_TIMEOUT_SECONDS,
        ),
        # A sourcing run scores every eligible candidate in the tenant and
        # then spends a model call on the top of that list, so its size is
        # the agency's database rather than anything the caller chose. The
        # timeout is what keeps a large roster from holding a worker slot
        # indefinitely, and a run it cuts short is left at `running` for
        # `rescan_stuck` to re-enqueue. `name` is given explicitly for the
        # same reason as above: producers enqueue the string "run_sourcing".
        func(
            run_sourcing,
            name="run_sourcing",
            timeout=settings.SOURCING_JOB_TIMEOUT_SECONDS,
        ),
        # A discovery scan is header pages, but as many of them as the
        # recruiter's mailbox holds for the window — the timeout keeps one
        # enormous mailbox from holding a worker slot indefinitely. A run it
        # cuts short is left `running`, goes stale, and the next scan
        # supersedes it (nothing sweeps discovery runs — the retry is a
        # click). `name` is explicit for the same reason as its siblings.
        func(
            run_client_discovery,
            name="run_client_discovery",
            timeout=settings.CLIENT_DISCOVERY_JOB_TIMEOUT_SECONDS,
        ),
        # Embedding a CV is one provider call, bounded by `EMBEDDING_MAX_CHARS`
        # so a very long CV cannot dominate the timeout. `name` is explicit and
        # matches the constant in `embedding_jobs.JOB_COMPUTE_EMBEDDING`:
        # producers enqueue the string "compute_candidate_embedding", and a
        # wrapper registered under any other name would fail on the far side of
        # the queue. The job is a no-op when embeddings are not configured, so
        # a deployment that has not opted in pays only the queue dispatch.
        func(
            compute_candidate_embedding,
            name="compute_candidate_embedding",
            timeout=settings.EMBEDDING_TIMEOUT_SECONDS,
        ),
        # The Job Intelligence analysis: three DeepSeek calls (understand →
        # persona → search) in the worker, where DeepSeek is configured — the
        # api process has no LLM credentials, so the first (synchronous) cut
        # fell back to OpenRouter and 400'd. `name` is explicit for the same
        # reason as its siblings: producers enqueue the string
        # "run_job_intelligence", and a wrapper under any other name fails on
        # the far side of the queue. Shared with the interactive worker
        # registry above, so the timeout cannot drift between the two.
        _JOB_INTELLIGENCE_FUNC,
        # The Candidate Intelligence analysis: three DeepSeek calls (career →
        # capability → profile) in the worker, the same shape Job Intelligence
        # takes. `name` is explicit for the same reason as its siblings:
        # producers enqueue the string "run_candidate_intelligence", and a
        # wrapper under any other name fails on the far side of the queue.
        _CANDIDATE_INTELLIGENCE_FUNC,
    ]
    # Every function above but the two classification jobs ends in a Graph call. Said once
    # here rather than discovered one failed job at a time.
    on_startup = staticmethod(_announce)
    redis_settings = redis_settings()
    poll_delay = settings.ARQ_POLL_DELAY_SECONDS
    max_jobs = settings.ARQ_MAX_JOBS
    max_tries = settings.ARQ_MAX_TRIES
