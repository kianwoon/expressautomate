"""The notification-delivery job (plan §10).

Split out of `jobs.py` when that module hit the 1500-line ceiling — this is
the one job family in it that touches no mailbox and no Graph call, so it is
the natural seam. The discipline is unchanged and documented on the job
itself: claim before sending, release on the unexpected, and let arq's retry
be driven by exceptions alone.

Registered in `app.workers.settings` alongside the jobs that stayed behind;
everything enqueues it by name, so nothing but that registry and the tests
import from here.
"""

import uuid
from datetime import UTC, datetime, timedelta

from arq import Retry
from sqlalchemy import text

from app.core.config import settings
from app.core.crypto import decrypt
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_WHATSAPP,
    CHANNEL_WHATSAPP_LINKED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_SUPPRESSED,
    address_digest,
)
from app.services.notify.candidate_events import CandidateEvent
from app.services.notify.channels import channel_for
from app.services.notify.channels.base import PermanentReason, SendOutcome
from app.services.notify.events import CANDIDATE_KIND_PREFIX, OpportunityEvent
from app.services.notify.render import render
from app.workers.queue import enqueue

log = get_logger(__name__)


class _TransientDeliveryFailure(Retry):
    """Raised by deliver_notification's transient-failure path only.

    That path has already moved the row back to 'pending' before raising, so
    the unexpected-exception guard around it must recognise this specific
    type and skip its own release step — otherwise a normal transient retry
    would take a second, redundant trip through _RELEASE_CLAIM on every
    failure.

    Subclasses arq's `Retry` — rather than a plain RuntimeError, which is what
    this was before finding 3 of the pre-merge review — so the provider's own
    `Retry-After` (already parsed into `SendResult.retry_after` by both
    channel clients) actually reaches arq's schedule instead of being
    discarded in favour of arq's default backoff. `fetch_email`'s
    `GraphThrottled` handling does the equivalent thing for Graph throttling;
    this is the delivery side's version. `retry_after=None` (the provider gave
    no hint) defers to arq's own default, same as before this fix.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(defer=retry_after)
        self._message = message

    def __str__(self) -> str:
        return self._message



# allow-hardcode: SQL statements, not a phrase list.

# The claim. Claiming *after* the send would double-message when the sweep and
# the original enqueue both fire for one row, which they can and do.
_CLAIM_DELIVERY = text(
    """
    UPDATE notification_deliveries
    SET status = :sending, attempts = attempts + 1
    WHERE id = :id AND status = :pending
    RETURNING id, destination_id, event_kind, subject_id, attempts, created_at
    """
)

# The backpressure re-queue: back to `pending` and the claim's own increment
# handed back. Undoing it here rather than not incrementing in the claim is
# deliberate — the claim cannot know yet whether the send will be a failure or
# a "not yet", and it must increment for every path that *is* a failure,
# including the ones that never return (a SIGKILLed worker, which is exactly
# what the attempt count exists to bound). GREATEST keeps a double-refund
# (this row re-queued by two racing paths) from producing a negative count.
_REQUEUE_UNCHARGED = text(
    """
    UPDATE notification_deliveries
    SET status = :pending, error = :error, attempts = GREATEST(attempts - 1, 0)
    WHERE id = :id
    """
)

_DELIVERY_TARGET = text(
    """
    SELECT d.channel, d.address_encrypted, d.failure_count, d.user_id
    FROM notification_destinations d
    WHERE d.id = :destination_id AND d.disabled_at IS NULL
    """
)

# The event is re-read at send time rather than carried through Redis: a job
# payload is not a place to put a job title, and the row is one join away.
_DELIVERY_SUBJECT = text(
    """
    SELECT job_title_raw, company_name_raw, location_raw, salary_raw
    FROM opportunities WHERE id = :opportunity_id
    """
)

# The candidate half of the same read. A `candidate.*` row's subject lives in
# a different table entirely, so the delivery job branches on the kind before
# it queries — pointing the opportunity query at a candidate id finds nothing
# and would fail the row as "subject no longer exists" on every share.
#
# `actor_name` and `note` are not re-read: they describe an act, not the
# candidate, and nothing stores them on a row this job can see. They are
# denormalised into the message only when the emitter renders inline.
_DELIVERY_CANDIDATE_SUBJECT = text(
    """
    SELECT full_name FROM candidates WHERE id = :candidate_id
    """
)

# Suppressed rows since this destination's last completed delivery. This is
# the "+N more" the next message carries. A plain SELECT, not an UPDATE: the
# count has to be read before the send (rendering needs it), and whether this
# batch actually gets to count as reported depends on whether the send that
# quotes it succeeds — read-then-maybe-mark, never claim-then-send. See
# _MARK_ROLLED_UP below for the write half.
_ROLLUP_IDS = text(
    """
    SELECT id FROM notification_deliveries
    WHERE destination_id = :destination_id
      AND event_kind = :event_kind
      AND status = :suppressed
    """
)

# Marks exactly the ids `_ROLLUP_IDS` returned earlier — not a fresh
# destination_id/event_kind scan — as accounted for. That is what keeps the
# rendered count and the rows actually retired in agreement even if a new
# suppressed row lands in the gap between the read and this write: a fresh
# scan here would silently sweep that row in as "reported" even though the
# message already sent never mentioned it. The `status = :suppressed` guard
# is defence in depth against the same row being retired twice (e.g. by a
# concurrent sweep) rather than the thing that decides which rows are in
# scope — the id list already fixed that.
#
# Run only alongside the SENT write, in the same transaction: a batch this
# call could not confirm delivered (send failed, threw, or the row's
# destination/subject vanished first) must remain `suppressed` so the next
# attempt or the recovery sweep still carries it. Marking it here regardless
# of outcome is finding 1 from the pre-merge review — the bug this shape
# fixes.
_MARK_ROLLED_UP = text(
    """
    UPDATE notification_deliveries
    SET status = :failed, error = 'rolled up'
    WHERE id = ANY(:ids) AND status = :suppressed
    RETURNING id
    """
)

# `sent_at` is computed in Python and passed as a plain boolean, rather than
# compared against `:status` twice in SQL — asyncpg deduces a single type per
# bind position, and reusing `:status` for both the SET and a CASE comparison
# produced "inconsistent types deduced for parameter $1" (text vs varchar).
_FINISH_DELIVERY = text(
    """
    UPDATE notification_deliveries
    SET status = :status, provider_message_id = :provider_message_id,
        error = :error,
        sent_at = CASE WHEN :is_sent THEN now() ELSE NULL END
    WHERE id = :id
    """
)

_RECORD_FAILURE = text(
    """
    UPDATE notification_destinations
    SET failure_count = failure_count + 1,
        disabled_at = CASE
            WHEN failure_count + 1 >= :max_failures THEN now() ELSE disabled_at
        END
    WHERE id = :id
    """
)

_RESET_FAILURES = text(
    "UPDATE notification_destinations SET failure_count = 0 WHERE id = :id"
)

# `whatsapp_suppressions` carries no tenant_id by design (see its model
# docstring) — an opt-out is a fact about our shared WhatsApp number, not
# about one agency — so this reads through the caller's own tenant_session
# rather than needing a SECURITY DEFINER function: the table's `global_read`
# policy (`USING (true)`) already permits the read regardless of which
# tenant is set.
_IS_SUPPRESSED = text(
    "SELECT 1 FROM whatsapp_suppressions WHERE address_hash = :address_hash"
)

# Used only by the unexpected-exception guard in deliver_notification. The
# `status = :sending` predicate is defence in depth: every terminal branch
# commits inside its own `tenant_session`, which rolls back on any exception
# (see app/db/rls.py), so a row can only still be 'sending' when this runs.
# Keeping the predicate anyway means a future refactor that breaks that
# invariant fails safe (no-op) instead of clobbering a row another path has
# since finished.
_RELEASE_CLAIM = text(
    """
    UPDATE notification_deliveries
    SET status = :pending
    WHERE id = :id AND status = :sending
    """
)


async def deliver_notification(ctx, *, delivery_id: str, tenant_id: str) -> None:
    """Send one outbox row.

    Claims before sending. The sweep and the original enqueue can both fire for
    the same row, and claiming afterwards would double-message; `RETURNING`
    with a `status = 'pending'` predicate makes the claim atomic, so the loser
    of the race gets no row and exits.

    A transient failure *raises*, because arq's retry is driven by exceptions.
    A permanent one does not — it disables the destination and returns, since
    retrying an address that will never accept a message is throughput spent on
    nothing.
    """
    tenant = uuid.UUID(tenant_id)

    async with tenant_session(tenant) as session:
        claimed = (
            await session.execute(
                _CLAIM_DELIVERY,
                {"id": delivery_id, "sending": STATUS_SENDING, "pending": STATUS_PENDING},
            )
        ).one_or_none()

    if claimed is None:
        # Already claimed, already sent, or owned by another tenant. RLS
        # already decided; there is nothing to do and nothing to report.
        log.info("delivery_skipped", delivery_id=delivery_id)
        return

    try:
        await _send_claimed_delivery(tenant, delivery_id, claimed)
    except _TransientDeliveryFailure:
        # The transient path below has already released the claim itself
        # (back to 'pending', so arq's retry or the sweep can pick it up).
        # Releasing again here would be a harmless no-op given the
        # `status = 'sending'` guard on _RELEASE_CLAIM, but re-raising
        # without touching the row keeps the intent obvious: this branch
        # is "already handled", not "needs handling".
        raise
    except Exception:
        # Anything else — a channel bug raising KeyError, a render bug, a
        # database error on one of the intermediate queries — is a failure
        # nobody anticipated. Every anticipated outcome above moves the row
        # out of 'sending'; an unanticipated one must not leave it stranded
        # there, because nothing else ever looks at 'sending' rows on its
        # own: arq's retry requires status = 'pending' to reclaim, and the
        # recovery sweep is what else scans 'sending' — deliberately, so a
        # worker killed by SIGKILL between claim and send (no exception
        # handler runs then) still has a row the sweep can reclaim, on top
        # of the 'pending' and 'suppressed' cases it also covers. Left
        # alone, this row would be lost silently and permanently.
        #
        # This cannot help if the worker is killed outright (SIGKILL, OOM,
        # container eviction) — no except block runs then, and the row is
        # still stuck. That case is the recovery sweep's job, not this one's.
        log.error(
            "delivery_unexpected_failure",
            delivery_id=delivery_id,
            exc_info=True,
        )
        async with tenant_session(tenant) as session:
            await session.execute(
                _RELEASE_CLAIM,
                {"id": delivery_id, "pending": STATUS_PENDING, "sending": STATUS_SENDING},
            )
        # Re-raise, not swallow: arq must see the exception to retry, and the
        # failure must be visible in logs/alerts, not just "a retry happened".
        raise


async def _send_claimed_delivery(tenant: uuid.UUID, delivery_id: str, claimed) -> None:
    """Render and send an already-claimed row.

    Split out of `deliver_notification` so the unexpected-exception guard
    there wraps exactly this — everything that happens after the claim
    succeeds and before any of it has committed a terminal status. Every
    `tenant_session` here commits or rolls back atomically (app/db/rls.py),
    so if any query in a terminal branch raises, that branch's own commit
    never happens and the row is still 'sending' when control returns to the
    guard above — safe to release. The one deliberate exception (transient
    failure) already released the row itself before raising; see
    `_TransientDeliveryFailure`.
    """
    async with tenant_session(tenant) as session:
        target = (
            await session.execute(
                _DELIVERY_TARGET, {"destination_id": claimed.destination_id}
            )
        ).one_or_none()

        if target is None:
            # Disabled between emit and delivery. Not a failure of this row.
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_FAILED,
                    "provider_message_id": None,
                    "error": "destination disabled",
                    "is_sent": False,
                },
            )
            return

        is_candidate = claimed.event_kind.startswith(CANDIDATE_KIND_PREFIX)
        subject = (
            await session.execute(
                _DELIVERY_CANDIDATE_SUBJECT
                if is_candidate
                else _DELIVERY_SUBJECT,
                {
                    ("candidate_id" if is_candidate else "opportunity_id"): (
                        claimed.subject_id
                    )
                },
            )
        ).one_or_none()

        # Read, not claimed, and fixed here as a concrete id list rather than
        # a count: rendering needs the count now, but whether this batch is
        # allowed to be reported as "accounted for" depends on whether the
        # send below actually succeeds. Fixing the ids now (rather than
        # re-deriving them by destination_id/event_kind after the send) is
        # what keeps the rendered "+N more" and the rows later marked
        # consumed in agreement even if another suppressed row lands for
        # this destination while the send is in flight — that new row is
        # simply left for the next delivery to count and report.
        rollup_ids = [
            row.id
            for row in (
                await session.execute(
                    _ROLLUP_IDS,
                    {
                        "destination_id": claimed.destination_id,
                        "event_kind": claimed.event_kind,
                        "suppressed": STATUS_SUPPRESSED,
                    },
                )
            ).all()
        ]

    if subject is None:
        # The opportunity was deleted after emit. Nothing to say about it.
        async with tenant_session(tenant) as session:
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_FAILED,
                    "provider_message_id": None,
                    "error": "subject no longer exists",
                    "is_sent": False,
                },
            )
        return

    event: OpportunityEvent | CandidateEvent
    if is_candidate:
        event = CandidateEvent(
            kind=claimed.event_kind,
            tenant_id=tenant,
            candidate_id=claimed.subject_id,
            candidate_name=subject.full_name,
        )
    else:
        event = OpportunityEvent(
            kind=claimed.event_kind,
            tenant_id=tenant,
            opportunity_id=claimed.subject_id,
            job_title=subject.job_title_raw,
            company_name=subject.company_name_raw,
            location=subject.location_raw,
            salary=subject.salary_raw,
        )
    content = render(event, target.channel, rollup=len(rollup_ids))
    address = decrypt(target.address_encrypted)

    if target.channel == CHANNEL_WHATSAPP:
        # Global by design: this person opted out of our *number*, which
        # every tenant shares. Messaging them again through a different
        # agency is exactly what Meta counts against the number, and the
        # quality rating it moves belongs to everyone, not just this tenant.
        async with tenant_session(tenant) as session:
            suppressed = (
                await session.execute(
                    _IS_SUPPRESSED, {"address_hash": address_digest(address)}
                )
            ).one_or_none()
        if suppressed is not None:
            async with tenant_session(tenant) as session:
                await session.execute(
                    _FINISH_DELIVERY,
                    {
                        "id": delivery_id,
                        "status": STATUS_FAILED,
                        "provider_message_id": None,
                        "error": "recipient opted out of WhatsApp messages",
                        "is_sent": False,
                    },
                )
            return

    if target.channel == CHANNEL_WHATSAPP_LINKED:
        # The only channel that sends on a *person's* socket rather than an
        # agency-wide account, so it is the only one that has to be told whose.
        channel = channel_for(
            target.channel, tenant_id=tenant, user_id=target.user_id
        )
    else:
        channel = channel_for(target.channel)
    result = await channel.send(address, content)

    if result.outcome is SendOutcome.SENT:
        async with tenant_session(tenant) as session:
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_SENT,
                    "provider_message_id": result.provider_message_id,
                    "error": None,
                    "is_sent": True,
                },
            )
            # A success clears the count, so three failures spread over a month
            # do not add up to a disabled destination.
            await session.execute(_RESET_FAILURES, {"id": claimed.destination_id})
            if rollup_ids:
                # Consumed in the same transaction as the SENT write, and
                # only here: this is the one outcome where the "+N more" this
                # message carried is actually true, so it is the only outcome
                # allowed to retire the batch it reported. Every other exit
                # from this function leaves these rows `suppressed` so a
                # retry or the recovery sweep still carries them.
                await session.execute(
                    _MARK_ROLLED_UP,
                    {
                        "ids": rollup_ids,
                        "suppressed": STATUS_SUPPRESSED,
                        "failed": STATUS_FAILED,
                    },
                )
        return

    if result.outcome is SendOutcome.PERMANENT:
        async with tenant_session(tenant) as session:
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_FAILED,
                    "provider_message_id": None,
                    "error": result.error,
                    "is_sent": False,
                },
            )
            if result.disable_destination:
                # Passed as 1, not the configured NOTIFY_MAX_FAILURES: a
                # permanent outcome that *is* about this address (bot-
                # blocked, undeliverable) means it is dead right now, so
                # disabling waits for nothing further to accumulate. The
                # configured threshold governs only the transient-exhaustion
                # path below, where several independent failures must add up
                # first.
                await session.execute(
                    _RECORD_FAILURE,
                    {"id": claimed.destination_id, "max_failures": 1},
                )
        if result.disable_destination:
            log.warning(
                "delivery_permanently_failed",
                delivery_id=delivery_id,
                channel=target.channel,
                error=result.error,
            )
        else:
            # Three different truths share this branch, and an operator can
            # only act on them if the log says which. What they have in common
            # is only that the address is fine, so the destination stays
            # enabled — disabling it would make a recruiter re-pair a device
            # that was never broken.
            _log_permanent_without_disable(delivery_id, target.channel, result)
        return

    # Transient, and the provider is only asking us to slow down. That is the
    # system working, not a delivery that failed, so it must not spend the
    # attempt budget: the WA gateway's spacing floor is tens of seconds, and an
    # evening batch of six enqueues at once means the last row would otherwise
    # burn all five attempts being told "not yet" and then report a real job
    # order as undeliverable. See SendResult.backpressure.
    if result.backpressure:
        age = datetime.now(UTC) - claimed.created_at
        deadline = timedelta(minutes=settings.NOTIFY_BACKPRESSURE_DEADLINE_MINUTES)
        if age < deadline:
            await _requeue_for_backpressure(tenant, delivery_id, claimed, result)
            return
        # Past the deadline the news is too stale to be worth sending, so this
        # does become a terminal failure — but deliberately WITHOUT
        # `_RECORD_FAILURE`. A provider that spaced us for half an hour says
        # nothing about this recruiter's address, and disabling their WhatsApp
        # because the gateway was busy would make them re-pair a device that
        # was never broken.
        async with tenant_session(tenant) as session:
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_FAILED,
                    "provider_message_id": None,
                    "error": (
                        "gave up after "
                        f"{int(age.total_seconds() // 60)} minutes of "
                        f"provider backpressure: {result.error}"
                    ),
                    "is_sent": False,
                },
            )
        # `error`, not warning: nothing retried its way out of this and a
        # recruiter is missing a notification, which is the same severity as a
        # channel-wide misconfiguration and needs the same operator attention.
        log.error(
            "delivery_backpressure_deadline_exceeded",
            delivery_id=delivery_id,
            channel=target.channel,
            age_seconds=int(age.total_seconds()),
            error=result.error,
        )
        return

    if claimed.attempts >= settings.NOTIFY_MAX_ATTEMPTS:
        async with tenant_session(tenant) as session:
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_FAILED,
                    "provider_message_id": None,
                    "error": f"gave up after {claimed.attempts} attempts: {result.error}",
                    "is_sent": False,
                },
            )
            await session.execute(
                _RECORD_FAILURE,
                {
                    "id": claimed.destination_id,
                    "max_failures": settings.NOTIFY_MAX_FAILURES,
                },
            )
        return

    async with tenant_session(tenant) as session:
        await session.execute(
            _FINISH_DELIVERY,
            {
                "id": delivery_id,
                "status": STATUS_PENDING,
                "provider_message_id": None,
                "error": result.error,
                "is_sent": False,
            },
        )
    # arq retries on an exception and on nothing else. Releasing the claim
    # first means the retry — or the sweep, whichever arrives — finds a row it
    # can claim rather than one stuck in `sending`. Raising the dedicated
    # subclass (rather than a plain RuntimeError) lets deliver_notification's
    # unexpected-exception guard recognise "already released, don't release
    # again" and just re-raise instead of re-running _RELEASE_CLAIM.
    raise _TransientDeliveryFailure(
        f"Transient notification failure: {result.error}",
        retry_after=result.retry_after,
    )


def _log_permanent_without_disable(delivery_id: str, channel: str, result) -> None:
    """One line per kind of "permanent, but the address is fine".

    Split by `permanent_reason` because the operator's next move differs, and a
    single event name asserting a configuration problem would be a claim we
    cannot support for two of the three — the plan's §15 "never invent" applies
    to log lines as much as to extracted fields.
    """
    if result.permanent_reason is PermanentReason.UNKNOWN:
        # The row now reads `failed`, and that status may simply be untrue —
        # the message may well have landed. Retrying is worse (a duplicate
        # nobody can un-send), so this is the best status available, but it is
        # the one outcome here where the database and reality can disagree.
        # `error` so it is countable: a rising rate means the gateway is losing
        # send confirmations, and nobody discovers that from `failed` rows.
        log.error(
            "delivery_outcome_unknown",
            delivery_id=delivery_id,
            channel=channel,
            error=result.error,
        )
        return
    if result.permanent_reason is PermanentReason.REJECTED:
        # The provider refused this one message on a healthy connection —
        # nothing is misconfigured and nothing is down, so there is no
        # operator action beyond noticing. `warning`, not `error`: paging
        # somebody for one rejected message would train them to ignore the
        # channel-wide case below.
        log.warning(
            "delivery_rejected_by_provider",
            delivery_id=delivery_id,
            channel=channel,
            error=result.error,
        )
        return
    # A configuration problem (e.g. an unapproved WhatsApp template). Every
    # send on this channel is failing the same way, so `log.error` is what
    # makes an operator notice before it reads as "every destination happened
    # to go quiet". Retrying will not help until the config is fixed.
    log.error(
        "delivery_permanently_failed_config",
        delivery_id=delivery_id,
        channel=channel,
        error=result.error,
    )


async def _requeue_for_backpressure(
    tenant: uuid.UUID, delivery_id: str, claimed, result
) -> None:
    """Hand the row back and book a fresh job for after the provider's wait.

    A *new* job rather than `_TransientDeliveryFailure`, which is the one place
    this path deliberately diverges from every other retry here. arq's retry
    keeps the job's identity and is capped by `ARQ_MAX_TRIES`, so re-queueing
    through it would swap the attempts ceiling this fix removes for arq's — the
    tail of a burst would stop failing and start stalling instead, waiting for
    `flush_notifications` to notice it minutes later. A fresh enqueue starts at
    try one, which is honest: nothing has been attempted yet.

    Returning normally (no exception) is the other half of that: arq must count
    this job as finished, not retried.
    """
    async with tenant_session(tenant) as session:
        await session.execute(
            _REQUEUE_UNCHARGED,
            {
                "id": delivery_id,
                "pending": STATUS_PENDING,
                "error": result.error,
            },
        )

    log.info(
        "delivery_deferred_for_backpressure",
        delivery_id=delivery_id,
        attempts=claimed.attempts - 1,
        retry_after=result.retry_after,
    )

    if not await enqueue(
        "deliver_notification",
        delivery_id=str(delivery_id),
        tenant_id=str(tenant),
        # arq accepts seconds here. `None` (the provider gave no hint) would be
        # "run it now", which is the one thing we know not to do, so fall back
        # to the sweep's own patience rather than hammering the provider.
        _defer_by=(
            result.retry_after
            if result.retry_after is not None
            else settings.NOTIFY_SWEEP_INTERVAL_SECONDS
        ),
    ):
        # The row is durable and `pending`, so `flush_notifications` is the net
        # — the same soft failure every other producer here relies on. Worth a
        # line because it is the difference between "sends in thirty seconds"
        # and "sends when the sweep gets to it".
        log.warning("delivery_backpressure_requeue_lost", delivery_id=delivery_id)
