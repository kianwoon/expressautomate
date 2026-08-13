"""Periodic recovery tasks for the supervisor process (plan §8, §9).

These run in `app/workers/main.py`, not in arq. The split is deliberate: arq
processes work, this process makes sure work exists to be processed. A wedged
arq worker still gets fresh work queued, and a crashed supervisor does not stop
work already in the queue.

They sweep across every tenant at once, so they have no single tenant
context to set and read through the narrow `SECURITY DEFINER` resolvers in the
`operator_resolvers` migration. Each returns routing ids only; the job that
follows re-reads the row under its own tenant policy.
"""

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.sync_event import (
    KIND_SUBSCRIPTION_RENEWED,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
)
from app.services.graph.subscriptions import renew_subscription
from app.services.ms_auth import MailboxNotAuthorised, TokenRefreshTransientError
from app.workers.jobs import (
    graph_client_for_mailbox,
    mark_needs_reauth,
    record_sync_event,
)
from app.workers.queue import enqueue

log = get_logger(__name__)

# Which job resumes a row stalled in each non-terminal status. The terminal
# ones are absent on purpose — `extracted`, `no_opportunity`, `skipped`,
# `unfetchable` and `failed` are outcomes, not interruptions, and requeueing
# them would loop forever on work that is already done.
RESUME_JOB = {
    "pending": "fetch_email",
    "fetched": "classify_email",
    # `classifying` means the gate was interrupted mid-call, so it resumes at
    # the gate. `classified` means the gate already answered and only the
    # extraction is missing — resuming *that* at the gate is what re-billed the
    # same email every RESCAN_WORKING_MINUTES for as long as extraction lagged.
    "classifying": "classify_email",
    "classified": "extract_email",
    "extracting": "extract_email",
    # A worker killed mid-replay leaves the email at `replaying`. It must
    # resume as a *replay*, not as a plain extraction: the row already exists,
    # and `extract_email` would no-op on it (`persist(replay=False)`), silently
    # discarding the improved values the replay exists to write.
    "replaying": "replay_email",
}

# allow-hardcode: SQL statement, not a phrase list.
# The replay half of extraction: emails whose latest extraction ran under an
# older prompt than the deployment now uses, claimed atomically so two sweeps
# cannot hand the same email to two workers. The claim writes, so it goes
# through a SECURITY DEFINER resolver like `claim_fetched_email_rows` — this
# sweep sets no `app.tenant_id`, and a direct UPDATE against `email_messages`
# (FORCE ROW LEVEL SECURITY) would match nothing at all, silently.
_CLAIM_REPLAY = text(
    "SELECT * FROM claim_replay_email_rows(:limit, :prompt_version)"
)

# allow-hardcode: SQL statements, not a phrase list.
_STALLED = text("SELECT * FROM stalled_email_rows(:pending_minutes, :working_minutes)")
_CLAIM_FETCHED = text("SELECT * FROM claim_fetched_email_rows(:limit)")
# The documents half of `rescan_stuck`. A resolver rather than a plain SELECT
# for the reason every statement here is one: this process sets no
# `app.tenant_id`, and `candidate_documents` carries FORCE ROW LEVEL
# SECURITY, so a direct read would match nothing at all — silently, since RLS
# filters rather than errors.
_STALLED_DOCUMENTS = text(
    "SELECT * FROM stalled_candidate_documents(:pending_minutes, :working_minutes)"
)
# The imports half-again of `rescan_stuck`, and a resolver for exactly the
# reason `_STALLED_DOCUMENTS` is: `candidate_imports` carries FORCE ROW LEVEL
# SECURITY and this process sets no `app.tenant_id`, so a direct read would
# match nothing at all — silently. Without this block a worker killed
# mid-import strands the uploaded file for ever: the upload route catches a
# `False` from `enqueue`, but a process that dies after taking the job
# returns nothing to catch.
_STALLED_IMPORTS = text(
    "SELECT * FROM stalled_candidate_imports(:pending_minutes, :working_minutes)"
)
# The job-description documents quarter of `rescan_stuck`, for the same reason
# `_STALLED_DOCUMENTS` is: `opportunity_documents` carries FORCE ROW LEVEL
# SECURITY and this sweep sets no `app.tenant_id`, so a direct read matches
# nothing.
_STALLED_OPPORTUNITY_DOCUMENTS = text(
    "SELECT * FROM stalled_opportunity_documents(:pending_minutes, :working_minutes)"
)
# The sourcing quarter of `rescan_stuck`, and a resolver for exactly the
# reason `_STALLED_IMPORTS` is: `sourcing_runs` carries FORCE ROW LEVEL
# SECURITY and this process sets no `app.tenant_id`, so a direct read would
# match nothing at all — silently. Without this block a worker killed
# mid-run leaves a recruiter watching a shortlist that says "running" and
# never arrives, with no way to ask for it again: the run row already exists,
# so a second request is a duplicate rather than a retry.
_STALLED_RUNS = text(
    "SELECT * FROM stalled_sourcing_runs(:pending_minutes, :working_minutes)"
)
# The Job Intelligence quarter of `rescan_stuck`, for the same reason
# `_STALLED_RUNS` exists: `job_intelligence` carries FORCE ROW LEVEL SECURITY
# and this sweep sets no `app.tenant_id`, so a direct read matches nothing.
_STALLED_INTELLIGENCE = text(
    "SELECT * FROM stalled_job_intelligence(:pending_minutes, :working_minutes)"
)
# The Candidate Intelligence quarter of `rescan_stuck`, for the same reason
# `_STALLED_INTELLIGENCE` exists: `candidate_intelligence` carries FORCE ROW
# LEVEL SECURITY and this sweep sets no `app.tenant_id`, so a direct read
# matches nothing.
_STALLED_CANDIDATE_INTEL = text(
    "SELECT * FROM stalled_candidate_intelligence(:pending_minutes, :working_minutes)"
)
_DUE_FOR_RENEWAL = text("SELECT * FROM subscriptions_due_for_renewal(:margin)")
_ACTIVE_MAILBOXES = text("SELECT * FROM active_mailboxes()")
_MISSING_SUBSCRIPTION = text("SELECT * FROM mailboxes_without_subscription()")
_AWAITING_BACKFILL = text("SELECT * FROM mailboxes_awaiting_backfill()")

# allow-hardcode: SQL statement, not a phrase list.
#
# `notification_deliveries` carries FORCE ROW LEVEL SECURITY (the
# `notifications` migration), and this sweep runs unscoped across every
# tenant with no session tenant_id set — a raw UPDATE here would match zero
# rows, silently, since RLS is a filter rather than an error. So this goes
# through `flush_notification_deliveries`, a narrow SECURITY DEFINER function
# (the `flush_notification_deliveries` migration) — the same pattern
# `stalled_email_rows` and `claim_fetched_email_rows` use above.
#
# Three kinds of row, one function, because they all need the same
# treatment: an enqueue that must happen and did not.
#
# `pending` past the stale window means the enqueue was lost — `enqueue`
# fails soft after the transaction committed, so the row is durable and the
# job is not. `suppressed` past the cap window means the rate cap ate a
# message and no later delivery arrived to carry its "+N more", so the batch
# would otherwise go unmentioned forever. `sending` past the stale window
# means `deliver_notification`'s claim was never released — the worker that
# claimed it was killed outright (SIGKILL, OOM, container eviction) before
# any of its exception handlers ran, so nothing moved the row back to
# `pending`. Nothing else ever looks at a `sending` row: arq's retry requires
# `status = 'pending'` to reclaim, so without this branch the row is lost
# silently and permanently.
#
# The `sending` case is gated on `updated_at`, not `created_at`, inside the
# function: the claim UPDATE is what moves a row into `sending`, and
# `touch_updated_at()` (bound on this table too) stamps `updated_at` on every
# UPDATE, so it measures time-since-claimed, not time-since-created. A row can
# sit `pending` for most of the stale window before a worker claims it —
# gating on `created_at` there would let this sweep repromote it while the
# worker that just claimed it is still very much alive, and a second worker
# could then win the `status = 'pending'` claim race in `deliver_notification`
# and double-message the recruiter. Gating on `updated_at` means the clock
# restarts at the claim, so the same stale-minutes window used for `pending`
# is the right one here too: a `deliver_notification` call is a single
# outbound API request that should finish in seconds, comfortably inside a
# window sized for the worst realistic *queue* latency. A worker merely slow,
# not dead, should never hold a claim anywhere near this long.
_FLUSHABLE_DELIVERIES = text(
    "SELECT * FROM flush_notification_deliveries(:stale_minutes, :limit)"
)

# P5 (plan §6, riskiest-thing test aside): a `pending` `candidate_activities`
# row (`20260729_2300_wa_send_pending_and_spacing.py`) older than the bound is
# a dispatch whose owning process died before it could resolve the row — see
# `sweep_stale_wa_sends` (`20260729_2330_sweep_stale_wa_sends.py`) for why
# this goes through a SECURITY DEFINER function rather than a raw UPDATE.
_STALE_WA_SENDS = text("SELECT * FROM sweep_stale_wa_sends(:stale_minutes, :limit)")

# allow-hardcode: bound on one sweep call, same reasoning as
# NOTIFY_FLUSH_LIMIT — the function itself also caps at 500.
_WA_SWEEP_LIMIT = 200

# A `pending` discovery run past this window never was claimed: the enqueue
# was lost after the row committed, or the queue consumer died before taking
# the job — either way no worker will ever pick it up (arq's retry only
# resumes rows a job actually claimed). A `running` run past the window was
# abandoned by a worker killed outright. `sweep_stale_client_discovery_runs`
# (`20260812_1000_sweep_stale_client_discovery.py`) is a SECURITY DEFINER
# function for the same reason every RLS-protected sweep here is: this process
# sets no `app.tenant_id`, so an unscoped UPDATE would match zero rows,
# silently. `_STALE_DISCOVERY_LIMIT` bounds one call, same as `_WA_SWEEP_LIMIT`.
_STALE_DISCOVERY_RUNS = text(
    "SELECT * FROM sweep_stale_client_discovery_runs(:stale_minutes, :limit)"
)
_DISCOVERY_SWEEP_LIMIT = 200


async def sweep_stale_client_discovery_runs() -> int:
    """Park every client-discovery run no worker is ever going to finish.

    A discovery run is a user-facing button, but that does not mean it can
    sit unclaimed forever: a `pending` row whose enqueue was lost (the row
    committed, the job never did) and a `running` row whose worker died
    (SIGKILL, OOM, eviction) before any exception handler could run both have
    no other owner. arq's own retry covers only rows a job actually claimed
    and only while arq still holds the job; a row whose enqueue never landed,
    or whose job arq has already given up on, would otherwise stay
    `pending`/`running` until the recruiter happened to scan again.

    Both become `failed` — never `unknown`, which is the WA send sweep's
    terminal state: a discovery run has no externally-observable half, so "we
    do not know" would be a lie. The run simply did not finish, and "scan
    again" is the truthful, actionable answer.

    The function's `WHERE status = ... AND clock < bound` is the
    compare-and-set that makes this safe to race the scan POST and the job
    itself: it can only touch a row still in the swept state and still old
    enough — never one a worker just claimed (the claim writes `updated_at`),
    never one that just started, never one already settled.

    `pending` gates on `created_at`, `running` on `updated_at`, exactly as
    `flush_notification_deliveries` splits its branches: a row can sit
    `pending` for most of the window before a worker claims it, and gating
    that branch on the claim write would be measuring the wrong clock.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                _STALE_DISCOVERY_RUNS,
                {
                    "stale_minutes": settings.CLIENT_DISCOVERY_STALE_PENDING_MINUTES,
                    "limit": _DISCOVERY_SWEEP_LIMIT,
                },
            )
        ).all()
        await session.commit()

    if rows:
        # Every one of these is a run the normal path should have carried and
        # did not — worth seeing in logs even though the run row's own `failed`
        # state is the user-visible half of this.
        log.warning("client_discovery_runs_swept", count=len(rows))
    return len(rows)


async def rescan_stuck() -> int:
    """Re-enqueue rows no worker is going to pick up on its own.

    This is the outbox net. Redis cannot join the Postgres transaction that
    committed the row, so an enqueue that failed after commit leaves durable
    work with no job attached. Without this sweep, "killing any worker
    mid-flight loses no email" is simply false.

    Four kinds of row, one function. Email messages, uploaded CVs, candidate
    imports, sourcing runs, Job Intelligence analyses and Candidate Intelligence
    analyses have nothing in common except the way they are stranded — a lost
    enqueue, or a worker killed mid-job — and that is the whole question this
    answers. A second scheduled task would be a second thing to forget to
    schedule, and the sweep that was forgotten is invisible until somebody
    notices a CV that has said "parsing"
    for a week.
    """
    ages = {
        "pending_minutes": settings.RESCAN_PENDING_MINUTES,
        "working_minutes": settings.RESCAN_WORKING_MINUTES,
    }
    async with SessionLocal() as session:
        rows = (await session.execute(_STALLED, ages)).all()
        documents = (await session.execute(_STALLED_DOCUMENTS, ages)).all()
        imports = (await session.execute(_STALLED_IMPORTS, ages)).all()
        opportunity_documents = (
            await session.execute(_STALLED_OPPORTUNITY_DOCUMENTS, ages)
        ).all()
        runs = (await session.execute(_STALLED_RUNS, ages)).all()
        intelligence = (await session.execute(_STALLED_INTELLIGENCE, ages)).all()
        candidate_intel = (
            await session.execute(_STALLED_CANDIDATE_INTEL, ages)
        ).all()

    requeued = 0
    for row in rows:
        job = RESUME_JOB.get(row.processing_status)
        if job is None:
            continue
        # Every job carries its tenant. Enqueueing only the row id would raise
        # TypeError inside the worker — on the far side of the queue, where
        # nothing is watching — and the recovery net would quietly do nothing.
        if await enqueue(
            job,
            email_message_id=str(row.id),
            tenant_id=str(row.tenant_id),
            mailbox_id=str(row.mailbox_id),
        ):
            requeued += 1

    for row in documents:
        # A document resumes at the job its state belongs to. `ingest_pending`
        # and `ingesting` are the front half — the identity read and candidate
        # resolution — and route to `ingest_candidate_cv`; `pending` and
        # `parsing` are the roles/skills parse and route to `parse_candidate_cv`
        # exactly as before. The resolver now returns `parse_state` (and
        # `origin`) so this routing needs no second read; a row is never sent to
        # the wrong job, which would re-read identity on a document mid-parse or
        # re-parse a document that has no candidate resolved yet.
        #
        # `candidate_id` is passed ONLY to `parse_candidate_cv`. The ingest
        # job's signature is `(tenant_id, document_id)` — it reads the row and
        # re-resolves the person itself, and a `candidate_id` keyword there is
        # a TypeError on the far side of the queue, which would strand the CV
        # every sweep re-enqueues it (a row a killed worker left at `ingesting`
        # is exactly the case this block exists to recover).
        if row.parse_state in ("ingest_pending", "ingesting"):
            if await enqueue(
                "ingest_candidate_cv",
                tenant_id=str(row.tenant_id),
                document_id=str(row.id),
            ):
                requeued += 1
        elif await enqueue(
            "parse_candidate_cv",
            tenant_id=str(row.tenant_id),
            candidate_id=str(row.candidate_id),
            document_id=str(row.id),
        ):
            requeued += 1

    for row in opportunity_documents:
        # Same shape as the documents block: `extract_opportunity_document`
        # accepts `extracting` as well as `pending`, so a file a killed worker
        # left mid-extraction is picked up rather than skipped as already
        # answered. Re-running is safe by construction — the conditional claim
        # inside the job is what decides who proceeds.
        if await enqueue(
            "extract_opportunity_document",
            tenant_id=str(row.tenant_id),
            document_id=str(row.id),
        ):
            requeued += 1

    for row in imports:
        # Same shape as the documents block, and for the same reason:
        # `run_candidate_import` accepts `parsing` as well as `pending`, so a
        # file a killed worker left half-applied is picked up rather than
        # skipped as already answered. Applying an import twice is safe by
        # construction — a row that matched an existing candidate the first
        # time matches the same one again and writes the same values.
        if await enqueue(
            "run_candidate_import",
            tenant_id=str(row.tenant_id),
            import_id=str(row.id),
        ):
            requeued += 1

    for row in runs:
        # Same shape again: `run_sourcing` accepts `running` as well as
        # `pending`, so a run a killed worker abandoned is picked up rather
        # than skipped as already answered. Re-running is safe because the
        # conditional claim inside the job is what decides who proceeds, and
        # `attempts` is spent there — a job order that crashes the scorer
        # every time reaches `failed` instead of coming back for ever.
        if await enqueue(
            "run_sourcing",
            tenant_id=str(row.tenant_id),
            opportunity_id=str(row.opportunity_id),
            run_id=str(row.id),
        ):
            requeued += 1

    for row in intelligence:
        # `run_job_intelligence` accepts `running` as well as `pending`, for
        # the same reason `run_sourcing` does: a killed worker mid-analysis is
        # picked up rather than stranded. The conditional claim in the job is
        # what prevents two workers proceeding, and `attempts` is what bounds
        # the loop for a job that crashes every time.
        # `queue_name` matters here too: recovery must land on the same
        # interactive queue the original click used, or a backlog on the
        # default queue would starve the recovered analysis exactly as it
        # starved the first attempt.
        if await enqueue(
            "run_job_intelligence",
            queue_name=settings.ARQ_INTERACTIVE_QUEUE,
            tenant_id=str(row.tenant_id),
            opportunity_id=str(row.opportunity_id),
            row_id=str(row.id),
        ):
            requeued += 1

    for row in candidate_intel:
        # `run_candidate_intelligence` accepts `running` as well as `pending`,
        # for the same reason `run_job_intelligence` does: a killed worker
        # mid-analysis is picked up rather than stranded. The conditional claim
        # in the job is what prevents two workers proceeding, and `attempts` is
        # what bounds the loop for a job that crashes every time.
        if await enqueue(
            "run_candidate_intelligence",
            queue_name=settings.ARQ_INTERACTIVE_QUEUE,
            tenant_id=str(row.tenant_id),
            candidate_id=str(row.candidate_id),
            row_id=str(row.id),
        ):
            requeued += 1

    if requeued:
        # Worth noticing rather than silently absorbing: every row here is one
        # the normal path should have carried and did not.
        log.warning("rescan_stuck_requeued", count=requeued)
    return requeued


async def classify_fetched() -> int:
    """Group fetched mail into batches and hand each one to the gate.

    This is what replaced `fetch_email` enqueueing a classification per email.
    The gate is the highest-volume model call in the system, and most of what a
    single-email call pays for is the instructions repeated in front of it, so
    the saving is real and it only exists if the emails arrive together.

    Batches never span tenants. Every job carries one tenant and reads its rows
    under that tenant's policy; a mixed batch would silently read none of the
    rows belonging to the others — the correct outcome under RLS, and a
    completely invisible way to lose mail.

    The claim writes: `claim_fetched_email_rows` moves the rows to
    `classifying` in the statement that selects them, so the next tick cannot
    re-claim work already in flight and bill for it twice.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                _CLAIM_FETCHED, {"limit": settings.CLASSIFY_SWEEP_LIMIT}
            )
        ).all()
        # The claim is an UPDATE, so it only takes effect on commit. Without
        # this the rows stay `fetched`, the next sweep claims them again, and
        # the batching saves nothing because every email is classified twice.
        await session.commit()

    by_tenant: dict[str, list[str]] = {}
    for row in rows:
        by_tenant.setdefault(str(row.tenant_id), []).append(str(row.id))

    size = settings.CLASSIFIER_BATCH_SIZE
    batches = 0
    for tenant_id, ids in by_tenant.items():
        for start in range(0, len(ids), size):
            await enqueue(
                "classify_batch",
                tenant_id=tenant_id,
                email_message_ids=ids[start : start + size],
            )
            batches += 1

    if rows:
        log.info("classify_batches_enqueued", emails=len(rows), batches=batches)
    return batches


async def replay_stale_extractions() -> int:
    """Re-read emails whose latest extraction ran under an older prompt.

    The systemic half of the structured-salary fix: a prompt upgrade only helps
    emails extracted *after* it lands, and old rows stay as the old prompt left
    them — which is how a "Contract Biotechnologist" email extracted before the
    structured salary bounds existed ended up with NULL salary columns. This
    sweep finds exactly those emails (latest extraction's `prompt_version` is
    not the one now configured), claims them to `replaying`, and enqueues
    `replay_email`, which re-extracts under the current prompt and refreshes the
    rows (`persist(replay=True)`).

    Bounded per sweep so a backlog drains gradually rather than paying for every
    historical email in one run. A replay that a worker crashes mid-flight is
    recovered by `rescan_stuck` — `replaying` maps to `replay_email` in
    `RESUME_JOB` — so nothing is stranded and nothing silently re-runs as a
    plain extraction.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                _CLAIM_REPLAY,
                {
                    "limit": settings.REPLAY_SWEEP_LIMIT,
                    "prompt_version": settings.PROMPT_VERSION,
                },
            )
        ).all()
        await session.commit()

    requeued = 0
    for row in rows:
        if await enqueue(
            "replay_email",
            email_message_id=str(row.id),
            tenant_id=str(row.tenant_id),
            mailbox_id=str(row.mailbox_id),
        ):
            requeued += 1

    if rows:
        log.info(
            "replay_extractions_claimed", emails=len(rows), requeued=requeued
        )
    return requeued


async def renew_subscriptions() -> int:
    """Renew before expiry, on the lifetime Graph actually granted.

    A subscription that cannot be renewed is replaced rather than left to
    lapse: a lapsed subscription stops delivering notifications and looks
    exactly like a quiet week.
    """
    async with SessionLocal() as session:
        due = (
            await session.execute(
                _DUE_FOR_RENEWAL,
                {"margin": settings.GRAPH_SUBSCRIPTION_RENEW_MARGIN},
            )
        ).all()

    renewed = 0
    for row in due:
        # One subscription's failure must not end the sweep. Acquiring the
        # client is inside the try for exactly that reason: it can raise
        # `MailboxNotAuthorised`, and `subscriptions_due_for_renewal` has no
        # mailbox-status filter, so one permanently dead grant would otherwise
        # abort at the same row every fifteen minutes and starve every
        # subscription behind it until they lapsed — silent mail loss, which is
        # the failure §8 exists to prevent.
        client = None
        try:
            client = await graph_client_for_mailbox(row.tenant_id, row.mailbox_id)
            await renew_subscription(row.tenant_id, row.subscription_id, client)
            renewed += 1
            # A renewal is a real action, not a poll: this sweep only touches
            # subscriptions inside the expiry margin, so a mailbox produces one
            # of these every few days rather than one every tick. It is also
            # the cheapest evidence the mailbox is still live — which is
            # exactly what someone reading a quiet panel is looking for.
            await record_sync_event(
                row.tenant_id,
                row.mailbox_id,
                KIND_SUBSCRIPTION_RENEWED,
                OUTCOME_SUCCEEDED,
                "Renewed the live feed from your mailbox",
            )
        except MailboxNotAuthorised as exc:
            # Recreating would need the same dead grant. Stop and tell the user.
            await mark_needs_reauth(row.tenant_id, row.mailbox_id, str(exc))
        except TokenRefreshTransientError:
            # Entra throttled or was slow — the grant is fine, so this is not
            # a reconnect. The sweep retries on its own clock
            # (RENEW_INTERVAL_SECONDS) and the subscription carries a full
            # half-life of slack (renewal falls at the midpoint), so skipping
            # is the whole of the fix. Flipping `needs_reauth` here is exactly
            # the false disconnect this classification exists to prevent.
            log.info(
                "subscription_renewal_transient",
                subscription_id=row.subscription_id,
            )
        except Exception:
            log.exception(
                "subscription_renewal_failed", subscription_id=row.subscription_id
            )
            # Recorded before the replacement is queued, so the panel shows the
            # failure even if `recreate_subscription` then succeeds quietly —
            # a feed that has to be rebuilt repeatedly is a pattern only the
            # history makes visible.
            await record_sync_event(
                row.tenant_id,
                row.mailbox_id,
                KIND_SUBSCRIPTION_RENEWED,
                OUTCOME_FAILED,
                "Renewing the live feed from your mailbox failed; rebuilding it",
            )
            await enqueue(
                "recreate_subscription",
                tenant_id=str(row.tenant_id),
                mailbox_id=str(row.mailbox_id),
            )
        finally:
            if client is not None:
                await client.aclose()

    return renewed


async def ensure_subscriptions() -> int:
    """Give an active mailbox a subscription if it somehow has none.

    This state is invisible to everything else — `renew_subscriptions` only
    scans subscriptions that exist, and `delta_sync_all` cares only about
    mailbox status — and it presents as a mailbox that looks entirely healthy
    and receives nothing. That is §8's "a lapsed subscription reads as a quiet
    week", reached by a different route.

    Two ways in, both real: a `recreate_subscription` whose create failed after
    the retire committed and whose retries were exhausted, and a mailbox
    reconnected after `needs_reauth`, whose old subscription was retired while
    nothing made a new one.
    """
    async with SessionLocal() as session:
        mailboxes = (await session.execute(_MISSING_SUBSCRIPTION)).all()

    for row in mailboxes:
        # `recreate_subscription` already does exactly this work, and its
        # retire step is a no-op when there is nothing active to retire.
        await enqueue(
            "recreate_subscription",
            tenant_id=str(row.tenant_id),
            mailbox_id=str(row.mailbox_id),
        )

    if mailboxes:
        log.warning("mailboxes_without_subscription", count=len(mailboxes))
    return len(mailboxes)


async def ensure_backfills() -> int:
    """Walk the history of any mailbox that never got its initial sync.

    `enqueue` never raises, so a Redis outage while someone is connecting a
    mailbox loses the backfill job and reports success. `ensure_subscriptions`
    then restores the subscription — new mail arrives, the mailbox looks
    entirely healthy — and the history they connected it *for* is never walked.

    Re-walking is safe: the dedup indexes make anything already held a no-op,
    and `backfill_mailbox` marks the mailbox once it finishes.
    """
    async with SessionLocal() as session:
        mailboxes = (await session.execute(_AWAITING_BACKFILL)).all()

    for row in mailboxes:
        await enqueue(
            "backfill_mailbox_job",
            tenant_id=str(row.tenant_id),
            mailbox_id=str(row.mailbox_id),
        )

    if mailboxes:
        log.info("mailboxes_awaiting_backfill", count=len(mailboxes))
    return len(mailboxes)


async def delta_sync_all() -> int:
    """Reconcile every active mailbox (plan §9).

    Fans out one job per mailbox rather than walking them here, so a single
    slow or throttled mailbox cannot hold up the sweep for everyone else.
    """
    async with SessionLocal() as session:
        mailboxes = (await session.execute(_ACTIVE_MAILBOXES)).all()

    for row in mailboxes:
        await enqueue(
            "delta_sync_mailbox",
            tenant_id=str(row.tenant_id),
            mailbox_id=str(row.mailbox_id),
        )
    return len(mailboxes)


async def flush_notifications() -> int:
    """Queue notifications nothing else is going to send.

    Runs unscoped, across every tenant at once, like the other sweeps here —
    hence the raw statement rather than a tenant session. Each row carries its
    own tenant, and the job re-reads it under that tenant's policy.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                _FLUSHABLE_DELIVERIES,
                {
                    "stale_minutes": settings.NOTIFY_DELIVERY_STALE_MINUTES,
                    "limit": settings.NOTIFY_FLUSH_LIMIT,
                },
            )
        ).all()
        # The promotion is an UPDATE and only takes effect on commit. Without
        # this the rows stay suppressed/sending and the next tick claims them
        # again.
        await session.commit()

    queued = 0
    for row in rows:
        if await enqueue(
            "deliver_notification",
            delivery_id=str(row.id),
            tenant_id=str(row.tenant_id),
        ):
            queued += 1

    if queued:
        # Worth noticing rather than silently absorbing: every row here is one
        # the normal path should have carried and did not.
        log.warning("notifications_flushed", count=queued)
    return queued


async def sweep_stale_wa_sends() -> int:
    """Resolve every WA gateway send whose owning process died mid-flight.

    Plan §6's liveness sweep, the pending-row half. A `pending` row this old
    was written by `POST /candidates/{id}/whatsapp-send`
    (`app/api/candidate_whatsapp.py#_claim_send`) before a dispatch that never
    came back — the request handler that would have resolved it is gone, so
    nothing else will ever move this row. It becomes `unknown`, and only
    `unknown` — never `failed`, because nobody observed a refusal (§15).

    `WA_SEND_STALE_PENDING_MINUTES` is set well above the gateway's own
    per-call timeout on purpose: a send that is merely slow, still genuinely
    in flight, must not be swept out from under the request that is about to
    resolve it. `sweep_stale_wa_sends`'s `WHERE status = 'pending'` is the
    compare-and-set that makes the two paths safe to race — whichever gets
    there first wins, and the loser touches nothing.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                _STALE_WA_SENDS,
                {
                    "stale_minutes": settings.WA_SEND_STALE_PENDING_MINUTES,
                    "limit": _WA_SWEEP_LIMIT,
                },
            )
        ).all()
        await session.commit()

    if rows:
        # Every one of these is a process that died holding a send — worth
        # seeing in logs even though the recruiter's own `unknown` row is the
        # user-visible half of this.
        log.warning("wa_sends_swept_to_unknown", count=len(rows))
    return len(rows)


# See `20260729_2400_wa_liveness_sweep.py` for the full reasoning; restated
# briefly: claim stamps `last_liveness_check_at = now()` up front so two
# concurrent sweeps cannot both pick the same session (the SKIP LOCKED
# compare-and-set), and returns each row's *status*, *previous* check time
# and the *claimed_at* stamp itself, so this module can (a) notice a
# gateway answer that disagrees with the row and repair it, and (b) revert
# the stamp on an unreachable gateway without racing a newer claim.
_WA_LIVENESS_CLAIM = text(
    "SELECT * FROM wa_sweep_claim_due_sessions(:stale_minutes, :limit)"
)
_WA_LIVENESS_REVERT = text(
    "SELECT wa_sweep_revert_check(:id, :previous, :claimed_at)"
)


async def sweep_wa_liveness() -> int:
    """Ask the gateway about every session that claims to be live.

    Plan §6, the gap this closes: `last_checked_at` on `GET /api/wa/session`
    only ever meant "when a browser last asked", so a session nobody has
    opened settings for in days looked current even if its socket died
    silently. This sweep is the background half — it asks, on a clock, so
    staleness is bounded even with no browser open.

    **This function never writes `wa_sessions.status` directly.** Per §6 the
    gateway's `POST /api/wa/internal/status` callback is meant to be the only
    thing that turns a gateway-reported status into a write, and it already
    fires whenever the gateway's own state changes — so simply calling
    `WaGatewayClient.status` is *usually* sufficient to bring the database up
    to date.

    It is not always sufficient. `gateway/src/callback.ts` is fire-and-forget
    — a push that fails to deliver is logged and dropped, never retried. If
    that lost push carried a status change, the row is wrong and stays wrong
    forever: asking `status()` again produces no new *change* on the gateway
    side, so no new callback is ever generated to fix it. Left alone, this
    sweep would be worse than not running at all — it would stamp
    `last_liveness_check_at` on a row it just confirmed is wrong. So when the
    gateway's answer disagrees with the status this task claimed, the task
    re-delivers that answer through `apply_internal_status` — the exact
    function `POST /api/wa/internal/status` calls, not a reimplementation of
    it (see that function's docstring for why this keeps §6's single-writer
    invariant intact rather than breaking it).

    Only `connected`/`reconnecting` sessions are ever claimed (enforced in
    `wa_sweep_claim_due_sessions`'s WHERE clause) — a `disconnected` or
    `logged_out` session has nothing to check, and asking would make the
    gateway wake a socket nobody asked it to hold open.

    **`last_liveness_check_at` is written only when the gateway actually
    answered** (§15). The claim function stamps it optimistically to win the
    SKIP LOCKED race; if the gateway call raises `GatewayUnreachableError` for
    that session, this function reverts the stamp to what it held before the
    claim (`wa_sweep_revert_check`, compare-and-set against the `claimed_at`
    this same claim wrote, so a slow revert can never clobber a newer claim).
    Leaving `now()` behind on a call that never got an answer would assert a
    check that did not happen — the old timestamp is the honest state,
    however stale it looks.
    """
    if not settings.wa_gateway_configured():
        # Same "a new service starts with zero env vars" reasoning as
        # `_call_gateway` in app/api/wa_gateway.py: nothing to check yet is a
        # real deployment state, not an error worth logging every tick.
        return 0

    from app.api.wa_gateway import InternalStatusIn, apply_internal_status
    from app.services.wa_gateway import GatewayUnreachableError, WaGatewayClient

    async with SessionLocal() as session:
        claimed = (
            await session.execute(
                _WA_LIVENESS_CLAIM,
                {
                    "stale_minutes": settings.WA_LIVENESS_CHECK_STALE_MINUTES,
                    "limit": settings.WA_LIVENESS_SWEEP_LIMIT,
                },
            )
        ).all()
        await session.commit()

    if not claimed:
        return 0

    client = WaGatewayClient()
    checked = 0
    unreachable = 0
    repaired = 0
    for row in claimed:
        try:
            snapshot = await client.status(str(row.tenant_id), str(row.user_id))
        except GatewayUnreachableError:
            unreachable += 1
            async with SessionLocal() as session:
                await session.execute(
                    _WA_LIVENESS_REVERT,
                    {
                        "id": row.id,
                        "previous": row.previous_check_at,
                        "claimed_at": row.claimed_at,
                    },
                )
                await session.commit()
            continue
        checked += 1
        if snapshot.status != row.status:
            # The row we claimed disagrees with what the gateway says right
            # now — a callback was lost. Re-deliver the gateway's own answer
            # through the single writer, so the row (and the SSE nudge that
            # follows) catches up. `status_detail`/`qr_expires_at` are not
            # known here — only `status` — so they are left unset rather than
            # guessed; the gateway's next real callback will fill them in.
            repaired += 1
            await apply_internal_status(
                InternalStatusIn(
                    tenant_id=row.tenant_id,
                    user_id=row.user_id,
                    status=snapshot.status,
                    phone_e164=snapshot.phone_number,
                )
            )

    if unreachable:
        log.warning(
            "wa_liveness_sweep_gateway_unreachable", checked=checked, unreachable=unreachable
        )
    if repaired:
        # A lost gateway callback, caught: worth seeing, since it is the
        # exact failure mode this sweep exists to close.
        log.warning("wa_liveness_sweep_repaired_lost_callback", repaired=repaired)
    return checked
