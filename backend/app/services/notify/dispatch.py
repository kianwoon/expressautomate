"""Event in, outbox rows out.

`emit` takes the caller's session rather than opening its own, so the
notification rows land in the *same* transaction that created the opportunity.
Either both commit or neither does — an opportunity with no notification row is
recoverable, but a notification for an opportunity that rolled back is a
message about something that never happened.

Enqueueing is deliberately separate and happens after that commit. A job that
starts before its row is visible reads nothing and exits, and would then never
be retried.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.notification import (
    STATUS_PENDING,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_SUPPRESSED,
)
from app.services.notify.events import OpportunityEvent
from app.workers.queue import enqueue

log = get_logger(__name__)

# allow-hardcode: SQL statements, not a phrase list.

# A destination only receives if it is verified, not disabled, and actively
# subscribed. All three in one statement so there is no window between the
# check and the insert.
_SUBSCRIBERS = text(
    """
    SELECT d.id AS destination_id, d.channel
    FROM notification_destinations d
    JOIN notification_subscriptions s ON s.destination_id = d.id
    WHERE s.event_kind = :event_kind
      AND s.active
      AND d.verified_at IS NOT NULL
      AND d.disabled_at IS NULL
    """
)

# `status = ANY(:statuses)` against ('pending', 'sending', 'sent') rather
# than `status <> 'suppressed'`. The cap exists to bound how many messages a
# person actually receives per hour, so it must count only rows that
# represent a real or in-flight delivery:
#   - pending / sending — still queued to actually go out;
#   - sent — actually delivered.
# `failed` rows are deliberately excluded even though they are not
# `suppressed`. That status covers two things that never reached anyone: a
# genuine delivery failure, and — since finding 1's fix — a suppressed batch
# marked 'failed'/'rolled up' once it was folded into a later message's "+N
# more" text. Counting either as cap-consuming traffic is exactly the
# phantom-traffic bug this predicate closes: after one suppression burst (or
# one run of real failures), a destination could be capped by messages nobody
# ever received. A row the recovery sweep repromotes from `suppressed` back
# to `pending` is likewise a message that never went out the first time, so
# it correctly falls under the `pending` branch here rather than double-
# counting — it is counted once, as what it now is: queued again.
_COUNT_RECENT = text(
    """
    SELECT count(*) FROM notification_deliveries
    WHERE destination_id = :destination_id
      AND event_kind = :event_kind
      AND created_at > now() - interval '1 hour'
      AND status = ANY(:statuses)
    """
)

# Bound once, not built as three OR'd literals in the SQL above, so the set of
# statuses that count towards the cap has one Python-side definition instead
# of drifting between the query text and whatever calls it.
_COUNTS_TOWARD_CAP = [STATUS_PENDING, STATUS_SENDING, STATUS_SENT]

# Re-reads status rather than trusting the caller's id list. `emit()` hands
# back every id it wrote — pending and suppressed alike, because a caller may
# need to look either kind up — so the only way to keep a suppressed row out
# of the queue for good is to check the row itself here, not at the call site.
_PENDING_AMONG = text(
    """
    SELECT id FROM notification_deliveries
    WHERE tenant_id = :tenant_id
      AND id = ANY(:ids)
      AND status = :status
    """
)

# ON CONFLICT DO NOTHING against the partial dedupe index: the extraction job
# can be retried, and the recruiter must not be told twice about one vacancy.
# The predicate here must match the index's `postgresql_where` verbatim
# (app/models/notification.py::ix_deliveries_dedupe) or Postgres rejects the
# clause as not matching any unique/exclusion constraint.
_INSERT_DELIVERY = text(
    """
    INSERT INTO notification_deliveries
        (id, tenant_id, destination_id, event_kind, subject_id, status)
    VALUES (:id, :tenant_id, :destination_id, :event_kind, :subject_id, :status)
    ON CONFLICT (destination_id, event_kind, subject_id)
      WHERE subject_id IS NOT NULL
      DO NOTHING
    RETURNING id
    """
)


async def rate_capped(
    session: AsyncSession, destination_id: uuid.UUID, event_kind: str
) -> bool:
    """Has this destination already had its hour's worth of this event?

    Counts the rows themselves rather than a stored counter, so the cap cannot
    drift from what was actually sent. Suppressed rows are excluded — counting
    them would make the first suppression permanent.
    """
    recent = (
        await session.execute(
            _COUNT_RECENT,
            {
                "destination_id": destination_id,
                "event_kind": event_kind,
                "statuses": _COUNTS_TOWARD_CAP,
            },
        )
    ).scalar_one()
    return recent >= settings.NOTIFY_RATE_CAP_PER_HOUR


async def _write_rows(
    event: OpportunityEvent, session: AsyncSession
) -> list[tuple[uuid.UUID, bool]]:
    """Insert one row per subscriber. Returns (delivery_id, capped) pairs for
    every row actually written — suppressed rows included, since a caller
    needs to tell "wrote nothing" (dedupe hit) apart from "wrote it, but
    suppressed" (rate cap hit).
    """
    subscribers = (
        await session.execute(_SUBSCRIBERS, {"event_kind": event.kind})
    ).all()

    written: list[tuple[uuid.UUID, bool]] = []
    for row in subscribers:
        capped = await rate_capped(session, row.destination_id, event.kind)
        delivery_id = (
            await session.execute(
                _INSERT_DELIVERY,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": event.tenant_id,
                    "destination_id": row.destination_id,
                    "event_kind": event.kind,
                    "subject_id": event.opportunity_id,
                    "status": STATUS_SUPPRESSED if capped else STATUS_PENDING,
                },
            )
        ).scalar_one_or_none()

        # None means the dedupe index rejected it — already told, nothing to do.
        if delivery_id is not None:
            written.append((delivery_id, capped))

    return written


async def emit(event: OpportunityEvent, session: AsyncSession) -> list[uuid.UUID]:
    """Write one outbox row per subscriber. Returns every id actually written.

    A row over the rate cap is written as `suppressed` rather than skipped: the
    next delivery counts them to say "and N more", and the sweep flushes them
    if no next delivery ever comes. Its id is still returned here — the caller
    (or a test) may need to look the row up — but `emit_and_enqueue` below
    knows not to queue a send job for it.
    """
    return [delivery_id for delivery_id, _ in await _write_rows(event, session)]


async def enqueue_deliveries(
    tenant_id: uuid.UUID, delivery_ids: list[uuid.UUID]
) -> int:
    """Queue only the ids that are still `pending`, verified by re-reading them.

    `emit()`'s contract is to return every id it wrote, suppressed rows
    included — a caller may need to look either kind up. That means this
    function cannot trust the id list it is handed; a caller that emits and
    enqueues in two separate steps (as the ingestion pipeline does) would
    otherwise queue the rate-capped rows too and defeat the cap entirely. So
    the filter lives here, against the rows themselves, rather than at each
    call site where it could be forgotten again.

    A row could in principle flip from pending to something else between this
    read and the `enqueue` call below, but not into a state that matters: rows
    are only marked suppressed at insert time (never after), and pending ->
    sent/failed happens once a worker has already picked the job up — in which
    case queuing it again is a harmless duplicate job, the same outcome the
    fail-soft sweep already tolerates. A row promoted back to pending by a
    recovery sweep is simply missed this round and picked up next.

    `enqueue` never raises and returns False on failure; the sweep is what
    turns a lost job back into a queued one, exactly as `rescan_stuck` does for
    ingestion.
    """
    if not delivery_ids:
        return 0

    try:
        async with tenant_session(tenant_id) as session:
            pending_ids = {
                row.id
                for row in (
                    await session.execute(
                        _PENDING_AMONG,
                        {
                            "tenant_id": tenant_id,
                            "ids": delivery_ids,
                            "status": STATUS_PENDING,
                        },
                    )
                ).all()
            }
    except Exception:
        log.exception("pending_status_check_failed", tenant_id=str(tenant_id))
        return 0

    queued = 0
    for delivery_id in delivery_ids:
        if delivery_id not in pending_ids:
            continue
        if await enqueue(
            "deliver_notification",
            delivery_id=str(delivery_id),
            tenant_id=str(tenant_id),
        ):
            queued += 1
    return queued


async def emit_and_enqueue(event: OpportunityEvent) -> int:
    """For callers with no transaction of their own. Opens, commits, enqueues.

    Only the non-suppressed ids go to the queue — a suppressed row has nothing
    to send yet, so queuing it would just make a worker fetch it and no-op.
    """
    async with tenant_session(event.tenant_id) as session:
        written = await _write_rows(event, session)
    to_enqueue = [delivery_id for delivery_id, capped in written if not capped]
    return await enqueue_deliveries(event.tenant_id, to_enqueue)
