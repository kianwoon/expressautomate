"""Periodic recovery tasks for the supervisor process (plan §8, §9).

These run in `app/workers/main.py`, not in arq. The split is deliberate: arq
processes work, this process makes sure work exists to be processed. A wedged
arq worker still gets fresh work queued, and a crashed supervisor does not stop
work already in the queue.

All three sweep across every tenant at once, so they have no single tenant
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
from app.services.ms_auth import MailboxNotAuthorised
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
}

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


async def rescan_stuck() -> int:
    """Re-enqueue rows no worker is going to pick up on its own.

    This is the outbox net. Redis cannot join the Postgres transaction that
    committed the row, so an enqueue that failed after commit leaves durable
    work with no job attached. Without this sweep, "killing any worker
    mid-flight loses no email" is simply false.

    Three kinds of row, one function. Email messages, uploaded CVs and
    candidate imports have nothing in common except the way they are stranded
    — a lost enqueue, or a worker killed mid-job — and that is the whole
    question this answers. A
    second scheduled task would be a second thing to forget to schedule, and
    the sweep that was forgotten is invisible until somebody notices a CV that
    has said "parsing" for a week.
    """
    ages = {
        "pending_minutes": settings.RESCAN_PENDING_MINUTES,
        "working_minutes": settings.RESCAN_WORKING_MINUTES,
    }
    async with SessionLocal() as session:
        rows = (await session.execute(_STALLED, ages)).all()
        documents = (await session.execute(_STALLED_DOCUMENTS, ages)).all()
        imports = (await session.execute(_STALLED_IMPORTS, ages)).all()

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
        # No resume map: a document has exactly one job, and both non-terminal
        # states resume at it. `parse_candidate_cv` accepts `parsing` as well
        # as `pending` precisely so a row a killed worker left mid-parse is
        # picked up rather than skipped as already answered.
        if await enqueue(
            "parse_candidate_cv",
            tenant_id=str(row.tenant_id),
            candidate_id=str(row.candidate_id),
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
