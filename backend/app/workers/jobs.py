"""arq jobs (plan §7, §10).

Fetch, classify and extract are separate jobs so their failure domains stay
separate: a Graph throttle must not cost an LLM call, and a bad model response
must not cost another Graph round trip. Each retries on its own terms.

**Every job carries its tenant in the payload.** Background work has no HTTP
request and therefore no session tenant, and the alternative — a second
`SECURITY DEFINER` function to look the tenant up — would widen the only part
of the system that bypasses RLS. Carrying it is also self-validating: a job
naming a mismatched (tenant, row) pair reads no row under the tenant policy and
quietly does nothing, which is exactly the desired outcome.

This module imports `enqueue` from `app.workers.queue`, and the arq registry
that imports both lives in `app.workers.settings` — importing either of those
from here would make the two modules mutually dependent.
"""

import uuid
from datetime import datetime
from urllib.parse import quote

from arq import Retry
from sqlalchemy import bindparam, text

from app.core.config import settings
from app.core.crypto import decrypt
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_WHATSAPP,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_SUPPRESSED,
    address_digest,
)
from app.models.sync_event import (
    KIND_BACKFILL,
    KIND_DELTA_SYNC,
    KIND_MAILBOX_REAUTH,
    KIND_SUBSCRIPTION_RECREATED,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
)
from app.services.graph.client import (
    MAILBOX_ROOT,
    GraphAuthError,
    GraphClient,
    GraphNotFound,
    GraphThrottled,
)
from app.services.graph.subscriptions import create_subscription, renew_subscription
from app.services.ms_auth import MailboxNotAuthorised, access_token_for_mailbox
from app.services.notify.channels import channel_for
from app.services.notify.channels.base import SendOutcome
from app.services.notify.events import OpportunityEvent
from app.services.notify.render import render
from app.services.storage.r2 import BodyStoreMisconfigured, R2BodyStore, body_key
from app.workers.queue import enqueue

log = get_logger(__name__)


class _TransientDeliveryFailure(RuntimeError):
    """Raised by deliver_notification's transient-failure path only.

    That path has already moved the row back to 'pending' before raising, so
    the unexpected-exception guard around it must recognise this specific
    type and skip its own release step — otherwise a normal transient retry
    would take a second, redundant trip through _RELEASE_CLAIM on every
    failure.
    """


# Only what is stored. Pulling the whole message would cost bandwidth on every
# fetch for fields nothing reads.
MESSAGE_FIELDS = (
    "id,internetMessageId,conversationId,subject,receivedDateTime,"
    "hasAttachments,from,body,bodyPreview"
)

# allow-hardcode: SQL statements, not a phrase list.
_CLAIM = text(
    "SELECT e.graph_message_id, e.processing_status, m.ms_user_id"
    " FROM email_messages e"
    " JOIN mailboxes m ON m.id = e.mailbox_id"
    " WHERE e.id = :id AND e.mailbox_id = :mailbox_id"
)

_RECORD_FETCH = text(
    """
    UPDATE email_messages SET
        internet_message_id = :internet_message_id,
        conversation_id = :conversation_id,
        sender_name = :sender_name,
        sender_email = :sender_email,
        subject = :subject,
        received_datetime = :received_datetime,
        has_attachments = :has_attachments,
        body_html_r2_key = :html_key,
        body_r2_key = :text_key,
        processing_status = 'fetched',
        attempt_count = attempt_count + 1,
        retention_until = now() + make_interval(
            days => (SELECT retention_months * 30 FROM mailboxes WHERE id = :mailbox_id)
        )
    WHERE id = :id
    """
)

_MARK_UNFETCHABLE = text(
    "UPDATE email_messages"
    " SET processing_status = 'unfetchable', source_state = 'deleted'"
    " WHERE id = :id"
)

_MARK_NEEDS_REAUTH = text("UPDATE mailboxes SET status = 'needs_reauth' WHERE id = :id")

_RETIRE_ACTIVE = text(
    "UPDATE graph_subscriptions SET status = 'replaced'"
    " WHERE mailbox_id = :mailbox_id AND status = 'active'"
)

_MAILBOX_TARGET = text(
    "SELECT ms_user_id, folder_id FROM mailboxes WHERE id = :mailbox_id"
)

_CLASSIFY_CLAIM = text(
    "SELECT processing_status, classification_status, body_html_r2_key, subject,"
    " sender_email"
    " FROM email_messages WHERE id = :id AND mailbox_id = :mailbox_id"
)

_START_CLASSIFYING = text(
    "UPDATE email_messages SET processing_status = 'classifying' WHERE id = :id"
)

_RECORD_CLASSIFICATION = text(
    """
    UPDATE email_messages SET
        classification_status = :status,
        classification_reason = :reason,
        classification_model = :model,
        classification_version = :version,
        -- `classified`, never back to `classifying`: the verdict is in, and
        -- only extraction is outstanding. Parking it at `classifying` is what
        -- made `rescan_stuck` re-run the gate on an already-answered row every
        -- fifteen minutes and enqueue a second extraction alongside it.
        processing_status = CASE WHEN :extract THEN 'classified' ELSE 'skipped' END,
        retention_until = CASE WHEN :extract THEN retention_until
                               ELSE now() + make_interval(days => :short) END
    WHERE id = :id
    """
)

# allow-hardcode: a SQL statement, not a phrase list.
_BATCH_CLAIM = text(
    "SELECT id, mailbox_id, processing_status, classification_status,"
    " body_html_r2_key, subject, sender_email"
    " FROM email_messages"
    " WHERE id IN :ids AND processing_status IN ('fetched', 'classifying')"
    # Deterministic, so a batch replayed after a crash sends its emails to the
    # model in the same order and the prompt is comparable across runs.
    " ORDER BY id"
).bindparams(bindparam("ids", expanding=True))

_EXTRACT_CLAIM = text(
    "SELECT processing_status, body_html_r2_key, subject, sender_email"
    " FROM email_messages WHERE id = :id AND mailbox_id = :mailbox_id"
)

_START_EXTRACTING = text(
    "UPDATE email_messages SET processing_status = 'extracting' WHERE id = :id"
)

_FINISH_EXTRACTION = text(
    "UPDATE email_messages SET processing_status = :status, last_error = NULL"
    " WHERE id = :id"
)

_FAIL_EXTRACTION = text(
    "UPDATE email_messages SET processing_status = 'failed', last_error = :error"
    " WHERE id = :id"
)

_BACKFILL_START = text(
    "SELECT initial_sync_from, backfill_completed_at"
    " FROM mailboxes WHERE id = :mailbox_id"
)


_RECORD_SYNC_EVENT = text(
    "INSERT INTO sync_events (id, tenant_id, mailbox_id, kind, outcome, detail)"
    " VALUES (:id, :tenant_id, :mailbox_id, :kind, :outcome, :detail)"
)

# The retention rule, run against one mailbox on every write. `ctid` rather
# than `id` keeps it a plain index scan plus a delete of the tail, and the
# OFFSET is what makes the cap exact: everything past the newest N goes.
_TRIM_SYNC_EVENTS = text(
    """
    DELETE FROM sync_events
    WHERE mailbox_id = :mailbox_id
      AND ctid NOT IN (
        SELECT ctid FROM sync_events
        WHERE mailbox_id = :mailbox_id
        ORDER BY created_at DESC, id DESC
        LIMIT :keep
      )
    """
)

# allow-hardcode: SQL statements, not a phrase list.

# The claim. Claiming *after* the send would double-message when the sweep and
# the original enqueue both fire for one row, which they can and do.
_CLAIM_DELIVERY = text(
    """
    UPDATE notification_deliveries
    SET status = :sending, attempts = attempts + 1
    WHERE id = :id AND status = :pending
    RETURNING id, destination_id, event_kind, subject_id, attempts
    """
)

_DELIVERY_TARGET = text(
    """
    SELECT d.channel, d.address_encrypted, d.failure_count
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


async def record_sync_event(
    tenant_id: uuid.UUID,
    mailbox_id: uuid.UUID | None,
    kind: str,
    outcome: str,
    detail: str,
) -> None:
    """Write one line of the dashboard's sync history — and never fail loudly.

    The whole body is wrapped because of what this function is: an audit trail
    for jobs that are themselves the recovery mechanism. A failed INSERT here
    raised inside `delta_sync_mailbox` would abort a sync that had already
    succeeded, arq would retry the *sync* to fix the *log*, and every ten
    minutes the mailbox would re-walk Graph because its diary was broken. The
    log describing an outage must not be able to cause one.

    Its own `tenant_session`, not the caller's: the caller's transaction may be
    the one that just failed, and an event recorded inside it would roll back
    with the failure it exists to report — which is the single case the panel
    matters most.

    `detail` is truncated for the same reason `last_error` is: it renders in a
    list in a browser, and an unbounded exception message would put a stack
    trace in it.
    """
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                _RECORD_SYNC_EVENT,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "mailbox_id": mailbox_id,
                    "kind": kind,
                    "outcome": outcome,
                    "detail": detail[:500],
                },
            )
            if mailbox_id is not None:
                await session.execute(
                    _TRIM_SYNC_EVENTS,
                    {
                        "mailbox_id": mailbox_id,
                        "keep": settings.SYNC_ACTIVITY_KEEP_PER_MAILBOX,
                    },
                )
    except Exception:
        # Logged, not raised. stdout is where this information lived before
        # this table existed, so losing the row costs the panel a line and
        # nothing else.
        log.exception("sync_event_not_recorded", kind=kind, outcome=outcome)


def body_store():
    """Indirection point, so tests can swap in the in-memory store."""
    return R2BodyStore()


async def graph_client_for_mailbox(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID
) -> GraphClient:
    return GraphClient(token=await access_token_for_mailbox(tenant_id, mailbox_id))


async def fetch_email(
    ctx, *, email_message_id: str, tenant_id: str, mailbox_id: str
) -> None:
    """Fetch one message from Graph and store its source (plan §7)."""
    tenant = uuid.UUID(tenant_id)
    mailbox = uuid.UUID(mailbox_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                _CLAIM, {"id": email_message_id, "mailbox_id": mailbox}
            )
        ).one_or_none()

    if row is None:
        # Unknown row, or a job whose tenant does not own it. RLS already
        # decided; there is nothing to do and nothing to report.
        log.info("fetch_skipped_unknown_row", email_message_id=email_message_id)
        return
    if row.processing_status != "pending":
        # `rescan_stuck` and the delta sweep may both enqueue the same row.
        # Doing the work twice is waste; doing it once is the point of this.
        log.info(
            "fetch_skipped_not_pending",
            email_message_id=email_message_id,
            status=row.processing_status,
        )
        return

    try:
        client = await graph_client_for_mailbox(tenant, mailbox)
    except MailboxNotAuthorised as exc:
        await mark_needs_reauth(tenant, mailbox, str(exc))
        return

    try:
        message = await client.get(
            _message_path(row.ms_user_id, row.graph_message_id),
            params={"$select": MESSAGE_FIELDS},
        )
    except GraphNotFound:
        # Gone before we ever saw the body. That source really is lost, so it
        # is recorded rather than retried into an exhausted job.
        await _unfetchable(tenant, email_message_id)
        return
    except GraphAuthError as exc:
        # 403 answers the same way forever. Retrying buries the cause.
        await mark_needs_reauth(tenant, mailbox, str(exc))
        return
    except GraphThrottled as exc:
        # arq only reschedules on `Retry`; a bare exception is a failed job and
        # the delay Graph asked for would be discarded.
        raise Retry(defer=exc.retry_after) from exc
    finally:
        await client.aclose()

    try:
        await _store(tenant, mailbox, email_message_id, row.graph_message_id, message)
    except BodyStoreMisconfigured as exc:
        # Every email fails here identically until an operator fixes it, so the
        # useful thing is a line naming what to fix rather than a botocore
        # traceback per message.
        #
        # Re-raised, not swallowed. The row stays `pending` — the status only
        # moves to `fetched` inside `_store` — and `rescan_stuck` re-enqueues
        # pending rows every RESCAN_PENDING_MINUTES, so the backlog drains on
        # its own once the bucket exists. Not `Retry`: this is not a delay
        # anybody can name, and the sweep is the recovery path that already
        # exists for it. Until then it costs one failed job and one line per
        # message per sweep, which is the noise floor of a broken deployment.
        log.error("body_store_misconfigured", mailbox_id=str(mailbox), detail=str(exc))
        raise

    # No classification job is enqueued here on purpose. The gate is batched —
    # one model call covers CLASSIFIER_BATCH_SIZE emails — so the row is left
    # at `fetched` for the `classify_fetched` sweep to claim alongside its
    # neighbours. Enqueueing per email is what batching exists to stop paying
    # for. The row is not stranded by this: `fetched` is a status both that
    # sweep and `rescan_stuck` pick up.


async def classify_email(
    ctx, *, email_message_id: str, tenant_id: str, mailbox_id: str
) -> None:
    """Decide whether this email is worth an extraction call (spec: Architecture).

    Failure discipline mirrors `fetch_email`: the gate itself never raises — it
    fails open to `uncertain` — so anything that escapes here is infrastructure
    (Postgres, R2), where no delay is worth naming and `Retry` would only burn
    arq attempts. The status is moved to `classifying` *before* the model call
    for that reason: a bare exception is a permanently failed job, and
    `rescan_stuck` re-enqueues `classifying` rows once the outage ends.
    """
    from app.services.ingest.classify import classify, should_extract
    from app.services.ingest.preprocess import to_text

    # Asked once, before the row is touched. The gate fails open by design, so
    # without this an unconfigured deployment would classify every email as
    # `uncertain`, pass all of them to extraction, and look like a working
    # system with a suspiciously indecisive model — the failure mode this
    # codebase keeps meeting, where a missing setting produces plausible
    # output instead of an error.
    if not settings.cerebras_configured(settings.CLASSIFIER_MODEL):
        log.error(
            "llm_not_configured",
            job="classify_email",
            detail="Set CEREBRAS_BASE_URL, CEREBRAS_API_KEY and CLASSIFIER_MODEL.",
        )
        raise RuntimeError("The classifier has no model configured.")

    tenant = uuid.UUID(tenant_id)
    mailbox = uuid.UUID(mailbox_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                _CLASSIFY_CLAIM, {"id": email_message_id, "mailbox_id": mailbox}
            )
        ).one_or_none()

    if row is None:
        # Unknown row, or a job whose tenant does not own it. RLS already
        # decided; there is nothing to do and nothing to report.
        log.info("classify_skipped_unknown_row", email_message_id=email_message_id)
        return
    # `classifying` is accepted, not just `fetched`: a worker killed mid-classify
    # leaves the row at `classifying`, and `rescan_stuck` re-enqueues exactly
    # this job for it. Accepting only `fetched` would make that row retry
    # forever, which is the failure this recovery path exists to prevent.
    if row.processing_status not in ("fetched", "classifying"):
        log.info(
            "classify_skipped_not_fetched",
            email_message_id=email_message_id,
            status=row.processing_status,
        )
        return
    # The verdict, not the pipeline status, is the authority on whether the
    # gate has already been paid for. Routing can be changed by a later edit —
    # this cannot: a row that has an answer is never worth asking about again,
    # and re-asking bills a model call for a result already stored.
    if row.classification_status != "unknown":
        log.info(
            "classify_skipped_already_classified",
            email_message_id=email_message_id,
            classification_status=row.classification_status,
        )
        return

    async with tenant_session(tenant) as session:
        await session.execute(_START_CLASSIFYING, {"id": email_message_id})

    html = await body_store().get(row.body_html_r2_key) or ""
    # `to_text` and not the raw HTML: it is the single source of truth for the
    # text extraction offsets index into, so the gate must judge the same
    # document the extractor will later quote from.
    body = to_text(html, subject=row.subject, sender=row.sender_email)
    verdict = await classify(body)

    async with tenant_session(tenant) as session:
        await session.execute(
            _RECORD_CLASSIFICATION,
            {
                "status": verdict.status,
                "reason": verdict.reason,
                "model": verdict.model,
                "version": settings.PROMPT_VERSION,
                "extract": should_extract(verdict.status),
                "short": settings.NON_RECRUITMENT_RETENTION_DAYS,
                "id": email_message_id,
            },
        )

    if should_extract(verdict.status):
        await enqueue(
            "extract_email",
            email_message_id=email_message_id,
            tenant_id=tenant_id,
            mailbox_id=mailbox_id,
        )


async def classify_batch(ctx, *, tenant_id: str, email_message_ids: list[str]) -> None:
    """Classify a claimed batch of emails in a single model call.

    The rows arrive already at `classifying`: `claim_fetched_email_rows` moved
    them there in the same statement that selected them, so a second sweep
    cannot hand the same emails to a second batch and pay for them twice. That
    also makes this job crash-safe without anything extra — a worker killed
    anywhere between the claim and the writes leaves its rows at `classifying`,
    which is exactly what `rescan_stuck` re-enqueues (as single-email
    `classify_email` jobs, so a batch that dies is retried email by email
    rather than as a batch that may be dying *because* of one of its members).

    Nothing here raises for a model failure: `classify_many` fails open, per
    email. What does escape is infrastructure — Postgres, R2 — where the rows
    stay at `classifying` and the sweep is the recovery path, the same
    discipline `classify_email` follows.
    """
    from app.services.ingest.classify import classify_many, should_extract
    from app.services.ingest.preprocess import to_text

    # Asked once, before any row is touched, for the reason `classify_email`
    # gives: the gate fails open, so an unconfigured deployment would mark
    # every email `uncertain`, send all of them to extraction, and look like a
    # working system with an indecisive model.
    if not settings.cerebras_configured(settings.CLASSIFIER_MODEL):
        log.error(
            "llm_not_configured",
            job="classify_batch",
            detail="Set CEREBRAS_BASE_URL, CEREBRAS_API_KEY and CLASSIFIER_MODEL.",
        )
        raise RuntimeError("The classifier has no model configured.")

    if not email_message_ids:
        return

    tenant = uuid.UUID(tenant_id)
    ids = [uuid.UUID(i) for i in email_message_ids]

    async with tenant_session(tenant) as session:
        rows = (await session.execute(_BATCH_CLAIM, {"ids": ids})).all()

    # Same authority as in `classify_email`, applied per member: one already
    # answered row in a replayed batch would otherwise be re-billed at the gate
    # and enqueued for a second extraction. Dropped here rather than in the
    # claim's WHERE so the skip is visible instead of silently narrowing.
    settled = [r for r in rows if r.classification_status != "unknown"]
    if settled:
        log.info(
            "classify_batch_skipped_already_classified",
            tenant_id=tenant_id,
            count=len(settled),
        )
        rows = [r for r in rows if r.classification_status == "unknown"]

    if not rows:
        # Every row was claimed by someone else, already finished, or belongs
        # to another tenant — RLS has already decided. Nothing to do.
        log.info("classify_batch_no_rows", tenant_id=tenant_id, asked=len(ids))
        return

    store = body_store()
    texts: list[str] = []
    for row in rows:
        html = await store.get(row.body_html_r2_key) or ""
        # `to_text`, not the raw HTML: it is the single source of truth for the
        # text extraction offsets index into, so the gate must judge the same
        # document the extractor will later quote from.
        texts.append(to_text(html, subject=row.subject, sender=row.sender_email))

    verdicts = await classify_many(texts)

    async with tenant_session(tenant) as session:
        for row, verdict in zip(rows, verdicts, strict=True):
            await session.execute(
                _RECORD_CLASSIFICATION,
                {
                    "status": verdict.status,
                    "reason": verdict.reason,
                    "model": verdict.model,
                    "version": settings.PROMPT_VERSION,
                    "extract": should_extract(verdict.status),
                    "short": settings.NON_RECRUITMENT_RETENTION_DAYS,
                    "id": row.id,
                },
            )

    # Enqueued after the writes commit, and only then: a job that started
    # before the status moved could read the row mid-flight, and one enqueued
    # for a row whose write failed would extract from an unclassified email.
    for row, verdict in zip(rows, verdicts, strict=True):
        if should_extract(verdict.status):
            await enqueue(
                "extract_email",
                email_message_id=str(row.id),
                tenant_id=tenant_id,
                mailbox_id=str(row.mailbox_id),
            )


async def extract_email(
    ctx, *, email_message_id: str, tenant_id: str, mailbox_id: str
) -> None:
    """Turn one classified email into verified vacancies (plan §12–§16).

    Failure discipline mirrors `classify_email`. The status moves to
    `extracting` *before* the model call, because arq only reschedules on
    `Retry` and nothing here raises one: an infrastructure failure is a
    permanently failed job, and `rescan_stuck` re-enqueues `extracting` rows
    once the outage ends (see RESUME_JOB in tasks.py). Leaving the row at
    `classified` across the call would instead let a second sweep pay for the
    same extraction while the first was still in flight.
    """
    from app.services.ingest.extract import extract
    from app.services.ingest.persist import persist
    from app.services.ingest.preprocess import to_text
    from app.services.llm.client import LLMInvalidJSON

    # Asked once, before the row is touched, for the reason `classify_email`
    # gives: an unconfigured deployment must fail loudly here rather than mark
    # every email `failed` one httpx error at a time.
    # `EXTRACTION_MODEL_STRONG` is not asked for: it is optional now, and
    # `extract` falls back to the fast model at a higher reasoning effort. A
    # guard demanding it would refuse a deployment that is perfectly able to
    # extract.
    if not settings.cerebras_configured(settings.EXTRACTION_MODEL_FAST):
        log.error(
            "llm_not_configured",
            job="extract_email",
            detail=(
                "Set CEREBRAS_BASE_URL, CEREBRAS_API_KEY and EXTRACTION_MODEL_FAST."
            ),
        )
        raise RuntimeError("Extraction has no model configured.")

    tenant = uuid.UUID(tenant_id)
    mailbox = uuid.UUID(mailbox_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                _EXTRACT_CLAIM, {"id": email_message_id, "mailbox_id": mailbox}
            )
        ).one_or_none()

    if row is None:
        # Unknown row, or a job whose tenant does not own it. RLS already
        # decided; there is nothing to do and nothing to report.
        log.info("extract_skipped_unknown_row", email_message_id=email_message_id)
        return
    # `extracting` is accepted alongside `classified`: a worker killed mid-call
    # leaves the row there, and `rescan_stuck` re-enqueues exactly this job for
    # it. Accepting only `classified` would strand that row forever.
    if row.processing_status not in ("classified", "extracting"):
        log.info(
            "extract_skipped_not_classified",
            email_message_id=email_message_id,
            status=row.processing_status,
        )
        return

    async with tenant_session(tenant) as session:
        await session.execute(_START_EXTRACTING, {"id": email_message_id})

    html = await body_store().get(row.body_html_r2_key) or ""
    # `to_text` is the single source of truth for the text the model's offsets
    # index into. Extracting from anything else — the raw HTML, the preview —
    # would make every span the model returns fail verification.
    source = to_text(html, subject=row.subject, sender=row.sender_email)

    try:
        response, result = await extract(source)
    except LLMInvalidJSON as exc:
        # Both models were asked and neither answered in the required shape.
        # Retrying would spend the same tokens on the same email for the same
        # result, so the row is marked and left for a human to look at.
        log.warning(
            "extraction_failed", email_message_id=email_message_id, error=str(exc)
        )
        await _fail_extraction(tenant, email_message_id, str(exc))
        return

    ids = await persist(tenant, uuid.UUID(email_message_id), response, result, source)
    # A recruitment email with no vacancy in it is a successful outcome, not a
    # failure — the gate fails open, so plenty of what reaches here genuinely
    # describes nothing to fill. Both statuses are terminal in RESUME_JOB.
    status = "extracted" if ids else "no_opportunity"

    async with tenant_session(tenant) as session:
        await session.execute(
            _FINISH_EXTRACTION, {"status": status, "id": email_message_id}
        )

    log.info(
        "extraction_recorded",
        email_message_id=email_message_id,
        opportunities=len(ids),
        model=result.model,
    )


async def _fail_extraction(
    tenant_id: uuid.UUID, email_message_id: str, error: str
) -> None:
    """Truncated: `last_error` is read by a human, and an unbounded model
    response would otherwise put a whole email body in a diagnostic column."""
    async with tenant_session(tenant_id) as session:
        await session.execute(
            _FAIL_EXTRACTION, {"error": error[:2000], "id": email_message_id}
        )


async def recreate_subscription(ctx, *, tenant_id: str, mailbox_id: str) -> None:
    """Replace a subscription that is gone or unrenewable (plan §8).

    The old row is retired first. Not for uniqueness — the new subscription has
    its own id, so nothing would collide — but because `resolve_subscription`
    routes on *active* rows: leaving the old one active would keep pointing
    notifications at a subscription Graph no longer has, and the renewal sweep
    would keep trying to extend it. Retiring after the create would instead
    need `_RETIRE_ACTIVE` to exclude the row just inserted.

    If the create fails after the retire commits, the mailbox is left with no
    active subscription. arq retries the job, and `ensure_subscriptions` is the
    backstop if those retries are exhausted.
    """
    tenant = uuid.UUID(tenant_id)
    mailbox = uuid.UUID(mailbox_id)

    try:
        client = await graph_client_for_mailbox(tenant, mailbox)
    except MailboxNotAuthorised as exc:
        # Recreating needs the grant that just failed. Nothing to retry.
        await mark_needs_reauth(tenant, mailbox, str(exc))
        return

    try:
        async with tenant_session(tenant) as session:
            target = (
                await session.execute(_MAILBOX_TARGET, {"mailbox_id": mailbox})
            ).one()
            await session.execute(_RETIRE_ACTIVE, {"mailbox_id": mailbox})

        await create_subscription(
            tenant, mailbox, target.ms_user_id, target.folder_id, client
        )
    except Exception as exc:
        # Recorded even though arq will retry: between the retire and a
        # successful create the mailbox receives no notifications, and that
        # window is invisible in every other surface the user has.
        await record_sync_event(
            tenant,
            mailbox,
            KIND_SUBSCRIPTION_RECREATED,
            OUTCOME_FAILED,
            f"Reconnecting the live feed from your mailbox failed: {exc!r}",
        )
        raise
    else:
        await record_sync_event(
            tenant,
            mailbox,
            KIND_SUBSCRIPTION_RECREATED,
            OUTCOME_SUCCEEDED,
            "Reconnected the live feed from your mailbox",
        )
    finally:
        await client.aclose()

    # Notifications stopped while the subscription was dead, so whatever
    # arrived in that window is reachable only through a delta walk.
    await enqueue("delta_sync_mailbox", tenant_id=tenant_id, mailbox_id=mailbox_id)


async def reauthorize_subscription(
    ctx, *, subscription_id: str, tenant_id: str, mailbox_id: str
) -> None:
    """Prove the grant still works, because Graph asked (plan §8).

    A successful token refresh *is* the proof, and renewing in place is what
    tells Graph so. If the grant is gone the user has to reconnect — retrying
    a revoked grant only buries the reason.
    """
    tenant = uuid.UUID(tenant_id)
    mailbox = uuid.UUID(mailbox_id)

    try:
        client = await graph_client_for_mailbox(tenant, mailbox)
    except MailboxNotAuthorised as exc:
        await mark_needs_reauth(tenant, mailbox, str(exc))
        return

    try:
        await renew_subscription(tenant, subscription_id, client)
    except GraphNotFound:
        # Graph dropped it while we were answering. Replacing it is the same
        # work `subscriptionRemoved` would have asked for.
        log.warning("reauthorized_subscription_absent", subscription_id=subscription_id)
        await enqueue(
            "recreate_subscription", tenant_id=tenant_id, mailbox_id=mailbox_id
        )
    finally:
        await client.aclose()


async def backfill_mailbox_job(ctx, *, tenant_id: str, mailbox_id: str) -> None:
    """The initial historical walk after a mailbox is connected (plan §6.2).

    Runs as a job rather than inline in the callback so a large mailbox cannot
    hold the user's browser on the OAuth redirect. It reads its own start date
    from the row, which the endpoint already clamped to the configured
    lookback.
    """
    from app.services.graph.delta import backfill_mailbox

    tenant = uuid.UUID(tenant_id)
    mailbox = uuid.UUID(mailbox_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(_BACKFILL_START, {"mailbox_id": mailbox})
        ).one_or_none()

    if row is None or row.initial_sync_from is None:
        # No mailbox, or no chosen start. Either way there is no window to walk.
        log.info("backfill_skipped_no_start", mailbox_id=mailbox_id)
        return

    if row.backfill_completed_at is not None:
        # Already walked. Reconnecting re-enqueues this, and re-walking the
        # whole lookback would be thousands of Graph calls for messages already
        # held — the delta sweep covers anything that arrived since.
        log.info("backfill_skipped_already_done", mailbox_id=mailbox_id)
        return

    since = row.initial_sync_from

    try:
        client = await graph_client_for_mailbox(tenant, mailbox)
    except MailboxNotAuthorised as exc:
        await mark_needs_reauth(tenant, mailbox, str(exc))
        return

    # Recorded unconditionally, unlike the delta sweep: a backfill happens once
    # per mailbox (twice if the user widens the window), and it is the event
    # the user is actually waiting on after connecting. "Imported 0 emails" is
    # a real answer to "where is my mail" — an empty delta poll is not.
    try:
        result = await backfill_mailbox(tenant, mailbox, client, since)
    except Exception as exc:
        await record_sync_event(
            tenant,
            mailbox,
            KIND_BACKFILL,
            OUTCOME_FAILED,
            f"Importing your mailbox history failed: {exc!r}",
        )
        raise
    else:
        await record_sync_event(
            tenant,
            mailbox,
            KIND_BACKFILL,
            OUTCOME_SUCCEEDED,
            f"Imported {result.recorded} of {result.seen} emails from your history"
            + (" (stopped at the import limit)" if result.capped else ""),
        )
    finally:
        await client.aclose()


async def delta_sync_mailbox(ctx, *, tenant_id: str, mailbox_id: str) -> None:
    """Reconcile one mailbox against Graph (plan §9).

    Runs on a schedule for every active mailbox and on demand after a `missed`
    lifecycle event. A dead grant stops it quietly rather than raising: this
    fires every ten minutes, and an exception each time would bury real
    failures under a repeating one.
    """
    from app.services.graph.delta import sync_mailbox

    tenant = uuid.UUID(tenant_id)
    mailbox = uuid.UUID(mailbox_id)

    try:
        client = await graph_client_for_mailbox(tenant, mailbox)
    except MailboxNotAuthorised as exc:
        await mark_needs_reauth(tenant, mailbox, str(exc))
        return

    try:
        result = await sync_mailbox(tenant, mailbox, client)
    except Exception as exc:
        # A failure is always worth a row. This is the sweep that keeps the
        # mailbox current, so it failing is precisely the thing the panel
        # exists to make visible.
        await record_sync_event(
            tenant,
            mailbox,
            KIND_DELTA_SYNC,
            OUTCOME_FAILED,
            f"Checking your mailbox for new email failed: {exc!r}",
        )
        raise
    finally:
        await client.aclose()

    # Deliberately *not* recorded when the sweep found nothing. This runs every
    # ten minutes for every active mailbox, and most polls are empty; a row per
    # poll would push a genuine failure off the panel within the hour and turn
    # the log into a heartbeat nobody reads. Success is only news when
    # something was actually imported — and a mailbox that has synced nothing
    # for days still shows its last real event, which is the honest picture.
    if result.recorded:
        await record_sync_event(
            tenant,
            mailbox,
            KIND_DELTA_SYNC,
            OUTCOME_SUCCEEDED,
            f"Synced {result.recorded} new email"
            f"{'' if result.recorded == 1 else 's'} from your inbox",
        )
        log.info(
            "delta_sync_recorded",
            mailbox_id=mailbox_id,
            recorded=result.recorded,
            seen=result.seen,
        )


def _message_path(ms_user_id: str, graph_message_id: str) -> str:
    """Build the message URL with the id encoded as a single path segment.

    Graph ids are base64-derived and can contain `/` and `+`. Interpolated
    raw, a `/` would split into extra path segments and the request would 404
    on a message that exists.

    Rooted at `MAILBOX_ROOT`, not `/users/{ms_user_id}`: the token is already
    this owner's, and `/users/{id}` does not work for personal accounts.
    """
    return f"{MAILBOX_ROOT}/messages/{quote(graph_message_id, safe='')}"


async def _store(
    tenant_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    email_message_id: str,
    graph_message_id: str,
    message: dict,
) -> None:
    """Write the body, then the row. The order is the whole point.

    A crash between the two costs one repeated write on retry, because the key
    is derived and the retry lands on it. The reverse order would leave a
    `fetched` row pointing at an object that was never written, and extraction
    would read nothing and record confident emptiness — a wrong answer rather
    than a visible failure.
    """
    html = (message.get("body") or {}).get("content") or ""
    plain = message.get("bodyPreview") or ""
    html_key = body_key(tenant_id, mailbox_id, graph_message_id, "html")
    text_key = body_key(tenant_id, mailbox_id, graph_message_id, "txt")

    store = body_store()
    await store.put(html_key, html)
    await store.put(text_key, plain)

    sender = ((message.get("from") or {}).get("emailAddress")) or {}
    async with tenant_session(tenant_id) as session:
        await session.execute(
            _RECORD_FETCH,
            {
                "internet_message_id": message.get("internetMessageId"),
                "conversation_id": message.get("conversationId"),
                "sender_name": sender.get("name"),
                "sender_email": sender.get("address"),
                "subject": message.get("subject"),
                "received_datetime": _parse_datetime(message.get("receivedDateTime")),
                "has_attachments": message.get("hasAttachments"),
                "html_key": html_key,
                "text_key": text_key,
                "mailbox_id": mailbox_id,
                "id": email_message_id,
            },
        )


async def _unfetchable(tenant_id: uuid.UUID, email_message_id: str) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(_MARK_UNFETCHABLE, {"id": email_message_id})
    log.info("fetch_source_gone", email_message_id=email_message_id)


async def mark_needs_reauth(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, reason: str
) -> None:
    """Stop ingesting and surface it, rather than retrying a dead grant.

    Public because the renewal sweep needs the same behaviour: a grant that
    cannot mint a token cannot renew a subscription either.

    The row is deliberately left `pending`: once the user reconnects,
    `rescan_stuck` picks it up and the email is fetched after all.
    """
    async with tenant_session(tenant_id) as session:
        await session.execute(_MARK_NEEDS_REAUTH, {"id": mailbox_id})
    log.warning("mailbox_needs_reauth", mailbox_id=str(mailbox_id), reason=reason[:200])
    # The one failure the user can actually fix, and the one that otherwise
    # reads as a quiet week: ingestion stops here until they reconnect.
    await record_sync_event(
        tenant_id,
        mailbox_id,
        KIND_MAILBOX_REAUTH,
        OUTCOME_FAILED,
        f"Microsoft stopped accepting our access to this mailbox — "
        f"reconnect it to resume: {reason}",
    )


def _parse_datetime(value: str | None) -> datetime | None:
    """Graph sends RFC 3339 with a `Z`, which fromisoformat rejects before 3.11
    and still renders as a naive datetime if the suffix is simply dropped."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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

        subject = (
            await session.execute(
                _DELIVERY_SUBJECT, {"opportunity_id": claimed.subject_id}
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

    result = await channel_for(target.channel).send(address, content)

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
            # A configuration problem (e.g. an unapproved WhatsApp template),
            # not a dead address — every send on this channel is failing the
            # same way, so `log.error` is what makes an operator notice
            # before it reads as "every destination happened to go quiet".
            # The destination is deliberately left enabled: retrying won't
            # help until the config is fixed, but the address itself is
            # fine, and disabling it would make every recruiter re-link
            # their number once the template lands.
            log.error(
                "delivery_permanently_failed_config",
                delivery_id=delivery_id,
                channel=target.channel,
                error=result.error,
            )
        return

    # Transient.
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
    raise _TransientDeliveryFailure(f"Transient notification failure: {result.error}")
