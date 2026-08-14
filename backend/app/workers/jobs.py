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

import hashlib
import uuid
from datetime import datetime
from urllib.parse import quote

from arq import Retry
from sqlalchemy import ARRAY, String, bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.sync_event import (
    KIND_BACKFILL,
    KIND_DELTA_SYNC,
    KIND_MAILBOX_REAUTH,
    KIND_SUBSCRIPTION_RECREATED,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
)
from app.services.events import KIND_MAILBOX, publish
from app.services.graph.client import (
    MAILBOX_ROOT,
    GraphAuthError,
    GraphClient,
    GraphNotFound,
    GraphThrottled,
)
from app.services.graph.subscriptions import create_subscription, renew_subscription
from app.services.ms_auth import (
    MailboxNotAuthorised,
    TokenRefreshTransientError,
    access_token_for_mailbox,
)
from app.services.storage.r2 import BodyStoreMisconfigured, R2BodyStore, body_key
from app.workers.queue import enqueue

log = get_logger(__name__)

# The pseudo-model recorded on a classification the trusted-sender skip
# answered without calling the LLM. It is what lets a report tell the free
# answers from the paid ones, and what stops the seed logic from re-trusting
# a domain it already trusted (a `trusted-sender` verdict is, by definition,
# already trusted).
TRUSTED_SENDER_MODEL = "trusted-sender"

# The pseudo-model recorded on a classification the deterministic noise rules
# answered (`gate_rules`). Same purpose as TRUSTED_SENDER_MODEL, for the rule
# half of the free answers.
GATE_RULE_MODEL = "gate-rule"


# Only what is stored. Pulling the whole message would cost bandwidth on every
# fetch for fields nothing reads.
MESSAGE_FIELDS = (
    "id,internetMessageId,conversationId,subject,receivedDateTime,"
    "hasAttachments,from,body,bodyPreview"
)

# allow-hardcode: SQL statements, not a phrase list.
_CLAIM = text(
    "SELECT e.graph_message_id, e.processing_status, m.ms_user_id,"
    " m.ingest_paused_at"
    " FROM email_messages e"
    " JOIN mailboxes m ON m.id = e.mailbox_id"
    " WHERE e.id = :id AND e.mailbox_id = :mailbox_id"
)

# The pause gate for delta_sync_mailbox, which otherwise never reads its
# mailbox row before walking. fetch_email gets the same column through _CLAIM.
_INGEST_PAUSED = text(
    "SELECT ingest_paused_at FROM mailboxes WHERE id = :mailbox_id"
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
        body_hash = :body_hash,
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
    " sender_email, body_hash"
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
    " body_html_r2_key, subject, sender_email, body_hash"
    " FROM email_messages"
    " WHERE id IN :ids AND processing_status IN ('fetched', 'classifying')"
    # Deterministic, so a batch replayed after a crash sends its emails to the
    # model in the same order and the prompt is comparable across runs.
    " ORDER BY id"
).bindparams(bindparam("ids", expanding=True))

# The hash-dedupe half of classification. An identical body is the same job
# order to the models that read it, so a verdict already recorded for that
# hash is the verdict this email would get — reusing it costs zero tokens.
# The query runs under the tenant policy, so a hash can only be answered by
# another email of the same agency. Rows the current batch holds are excluded,
# and the newest answer wins per hash.
# allow-hardcode: a SQL statement, not a phrase list.
_FIND_PRIOR_VERDICT = text(
    """
    SELECT body_hash, classification_status, classification_reason,
           classification_model
    FROM email_messages
    WHERE body_hash = ANY(:hashes)
      AND classification_status != 'unknown'
      AND NOT (id = ANY(:ids))
    ORDER BY updated_at DESC, id DESC
    """
).bindparams(bindparam("hashes", type_=ARRAY(String)), bindparam("ids", type_=ARRAY(PgUUID)))

_EXTRACT_CLAIM = text(
    "SELECT processing_status, body_html_r2_key, subject, sender_email"
    " FROM email_messages WHERE id = :id AND mailbox_id = :mailbox_id"
)

# Compare-and-set: only the worker that moves a `classified` row to
# `extracting` wins the claim. A second job that read the same `classified`
# row before the first wrote (two enqueues for one email, an arq retry landing
# while the first is still in flight) gets `claimed = false` and bows out,
# rather than paying for the same model call twice. A row already at
# `extracting` is left alone here — that is the recovery case below, handled
# by the status check above this runs.
_CLAIM_EXTRACTING = text(
    "UPDATE email_messages SET processing_status = 'extracting'"
    " WHERE id = :id AND processing_status = 'classified'"
    " RETURNING id"
)

_FINISH_EXTRACTION = text(
    "UPDATE email_messages SET processing_status = :status, last_error = NULL"
    " WHERE id = :id"
)

# The successful half of the replay budget: a replay that COMPLETED spent its
# pickup legitimately (the claim resolver moves the row to `replaying` before
# the job first sees it, so first-vs-recovery is not distinguishable there).
# Resetting on success means `llm_attempts` counts *failed* pickups — a
# healthy email replays once per prompt upgrade forever (the replay sweep's
# whole purpose), and only a row whose replays keep crashing burns toward
# `EMAIL_LLM_MAX_ATTEMPTS` and parks `failed`.
_RESET_LLM_ATTEMPTS = text(
    "UPDATE email_messages SET llm_attempts = 0 WHERE id = :id"
)

# The replay claim. `replay_stale_extractions` has already moved the email to
# `replaying` (claim_replay_email_rows, a SECURITY DEFINER resolver that runs
# with no tenant context); this job accepts exactly that state and none other,
# so a plain `extract_email` can never accidentally consume a replay claim and
# run it without the replay flag. A row stuck at `replaying` after a killed
# worker is re-enqueued by `rescan_stuck` (RESUME_JOB maps it here), which is
# what makes a mid-replay crash recoverable as a replay rather than as an
# ordinary extraction that would no-op on the already-existing rows.
_REPLAY_CLAIM = text(
    "SELECT processing_status, body_html_r2_key, subject, sender_email"
    " FROM email_messages WHERE id = :id AND processing_status = 'replaying'"
)

_FAIL_EXTRACTION = text(
    "UPDATE email_messages SET processing_status = 'failed', last_error = :error"
    " WHERE id = :id"
)

# The LLM-spend bound (migration c1v2n0000001). Compare-and-set, so only one
# worker's increment lands and a second job reading the same row sees the
# spent count. The WHERE statuses are exactly the sweep-recovery shapes: a
# row ALREADY in its working status is a recovery pickup (forward progress
# never re-enters a status it holds), and every `replaying` pickup counts
# because the replay claim resolver moves the row there before the job first
# sees it — a replay is deliberate spend every time. The first classify
# (fetched→classifying) and first extract (classified→extracting) do NOT
# match and spend nothing: the healthy path keeps its whole budget.
_CLAIM_LLM_ATTEMPT = text(
    """
    UPDATE email_messages SET llm_attempts = llm_attempts + 1
    WHERE id = :id
      AND processing_status IN ('classifying', 'extracting', 'replaying')
    RETURNING llm_attempts
    """
)


async def _llm_attempts_exhausted(tenant: uuid.UUID, email_message_id: str) -> bool:
    """Has this email spent its recovery budget for LLM-paying jobs?

    Spent at pickup, BEFORE any model call, exactly as the intelligence jobs
    spend their `attempts`: a worker killed mid-call never reaches an end, so
    a count spent at completion would count nothing on precisely the runs
    this bounds. Past the ceiling the row is parked `failed` — terminal, so
    `rescan_stuck` stops returning it and the rebill loop ends. A human
    re-runs from the UI (a fresh pickup of a `failed` row is still refused;
    the reset is a deliberate operator action on the row, not a sweep's).
    """
    async with tenant_session(tenant) as session:
        spent = (
            await session.execute(_CLAIM_LLM_ATTEMPT, {"id": email_message_id})
        ).scalar_one_or_none()
    if spent is None:
        # Unknown row — RLS already decided; treat as exhausted rather than
        # paying for a row this tenant does not own.
        return True
    return spent > settings.EMAIL_LLM_MAX_ATTEMPTS

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
    if row.ingest_paused_at is not None:
        # The owner switched intake off. This is the authoritative gate — the
        # webhook, the delta sweep and `rescan_stuck` all funnel through this
        # job, so nothing is fetched during a pause no matter which door the
        # notification came in by. The row stays `pending` deliberately: a row
        # that reaches here was recorded *before* the pause (the webhook drops
        # paused mailboxes' notifications and the delta walk is gated, so
        # nothing records rows during one), and `rescan_stuck` picks it up
        # once intake resumes — pre-pause mail is not the mail this feature
        # exists to skip.
        log.info(
            "fetch_skipped_intake_paused",
            email_message_id=email_message_id,
            mailbox_id=mailbox_id,
        )
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
    except TokenRefreshTransientError as exc:
        # Entra throttled or was slow — the grant is fine, so this is not a
        # reconnect. Retry on the same clock Graph itself names when
        # throttling; the row stays `pending`, and `rescan_stuck` re-enqueues
        # it if arq's retries are exhausted.
        raise Retry(defer=settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS) from exc

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
    if not settings.llm_provider_configured(settings.CLASSIFIER_MODEL):
        log.error(
            "llm_not_configured",
            job="classify_email",
            detail="Set LLM_PROVIDER_BASE_URL, LLM_PROVIDER_API_KEY and CLASSIFIER_MODEL.",
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
    # The recovery-budget gate, AFTER the verdict guard: a row the sweep hands
    # back still `classifying` but already answered spends nothing here — an
    # already-paid verdict is never worth budget. A row still unanswered is a
    # crash-loop iteration by definition, so it pays — and past the ceiling
    # the row is parked `failed` before the gate is asked anything.
    if row.processing_status == "classifying":
        if await _llm_attempts_exhausted(tenant, email_message_id):
            log.warning(
                "classify_attempts_exhausted",
                email_message_id=email_message_id,
            )
            await _fail_extraction(tenant, email_message_id, "classify attempts exhausted")
            return

    async with tenant_session(tenant) as session:
        await session.execute(_START_CLASSIFYING, {"id": email_message_id})

    html = await body_store().get(row.body_html_r2_key) or ""
    # `to_text` and not the raw HTML: it is the single source of truth for the
    # text extraction offsets index into, so the gate must judge the same
    # document the extractor will later quote from.
    body = to_text(html, subject=row.subject, sender=row.sender_email)

    # The trusted-sender skip is checked in the same transaction that starts
    # classifying, so a verdict written as `trusted-sender` and the trust row
    # that justified it cannot disagree.
    # Order of filters, cheapest first, mirroring classify_batch:
    #   1. deterministic noise rules (in-memory, zero cost)
    #   2. trusted sender domain (one indexed SELECT)
    #   3. identical body already classified (one query)
    #   4. the LLM gate (the only paid step)
    from app.services.ingest import gate_rules

    rule = gate_rules.gate_rule(subject=row.subject, sender_email=row.sender_email)
    if rule is not None:
        verdict = _rule_verdict(rule)
    else:
        async with tenant_session(tenant) as session:
            trusted = await _is_trusted(session, tenant, row.sender_email)
        if trusted:
            verdict = _trusted_verdict()
        else:
            # An identical body elsewhere in this tenant already has a verdict:
            # reusing it costs zero tokens (same reasoning as the batch path).
            prior = None
            if row.body_hash:
                async with tenant_session(tenant) as session:
                    prior = (
                        await session.execute(
                            _FIND_PRIOR_VERDICT,
                            {
                                "hashes": [row.body_hash],
                                "ids": [uuid.UUID(email_message_id)],
                            },
                        )
                    ).first()
            if prior is not None:
                verdict = _reused_verdict(prior)
            else:
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
        await _record_gate_usage(
            session,
            tenant=tenant,
            email_message_id=uuid.UUID(email_message_id),
            verdict=verdict,
        )
        # Seed trust only after a verdict the gate actually answered — a
        # trusted-sender verdict is already trusted, and an `uncertain` one is
        # the gate failing open (trusting it would trust a domain the gate
        # could not read). `mark_trusted_domain` is idempotent.
        if verdict.status == "recruitment" and verdict.model != TRUSTED_SENDER_MODEL:
            await _mark_trusted(session, tenant, row.sender_email)

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
    from app.services.ingest import gate_rules
    from app.services.ingest.classify import classify_many, should_extract
    from app.services.ingest.preprocess import to_text

    # Asked once, before any row is touched, for the reason `classify_email`
    # gives: the gate fails open, so an unconfigured deployment would mark
    # every email `uncertain`, send all of them to extraction, and look like a
    # working system with an indecisive model.
    if not settings.llm_provider_configured(settings.CLASSIFIER_MODEL):
        log.error(
            "llm_not_configured",
            job="classify_batch",
            detail="Set LLM_PROVIDER_BASE_URL, LLM_PROVIDER_API_KEY and CLASSIFIER_MODEL.",
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
    # Order of filters, cheapest first. Every filter is fail-open — a miss
    # just sends the email to the next filter:
    #   1. deterministic noise rules (in-memory, zero cost)  → non_recruitment
    #   2. trusted sender domain (one indexed SELECT)        → recruitment
    #   3. identical body already classified (one query)     → reuse verdict
    #   4. the LLM gate (the only paid step)                 → decides the rest
    rule_rows: list = []
    trusted_rows: list = []
    remaining_rows: list = []
    for row in rows:
        rule = gate_rules.gate_rule(subject=row.subject, sender_email=row.sender_email)
        if rule is not None:
            rule_rows.append((row, rule))
        else:
            remaining_rows.append(row)

    # Among the remaining, split out emails whose sender domain the gate
    # already trusts: they skip the model call entirely, and the emails that
    # reach the model are the only ones that share a batch. The trust check
    # reads under the tenant policy and fails open.
    trusted_rows = []
    untrusted_rows = []
    async with tenant_session(tenant) as session:
        for row in remaining_rows:
            if await _is_trusted(session, tenant, row.sender_email):
                trusted_rows.append(row)
            else:
                untrusted_rows.append(row)

    # Among the untrusted, split out emails whose body hash already has a
    # verdict elsewhere in this tenant: an identical body is the same job
    # order, so the recorded verdict is the one this email would get — reusing
    # it costs zero tokens. The check fails open: a missing hash, a broken
    # query, a hash with no prior verdict — all send the email to the model.
    hashed_rows = [r for r in untrusted_rows if r.body_hash]
    duplicate_rows: list = []
    fresh_rows: list = []
    hash_verdicts: dict[str, dict] = {}
    if hashed_rows:
        async with tenant_session(tenant) as session:
            prior = (
                await session.execute(
                    _FIND_PRIOR_VERDICT,
                    {
                        "hashes": [r.body_hash for r in hashed_rows],
                        "ids": [r.id for r in rows],
                    },
                )
            ).all()
        # Newest verdict wins per hash (the query is ordered by updated_at).
        for pr in prior:
            if pr.body_hash not in hash_verdicts:
                hash_verdicts[pr.body_hash] = pr
        for row in untrusted_rows:
            if row.body_hash and row.body_hash in hash_verdicts:
                duplicate_rows.append(row)
            else:
                fresh_rows.append(row)
    else:
        fresh_rows = untrusted_rows

    texts: list[str] = []
    for row in fresh_rows:
        html = await store.get(row.body_html_r2_key) or ""
        # `to_text`, not the raw HTML: it is the single source of truth for the
        # text extraction offsets index into, so the gate must judge the same
        # document the extractor will later quote from.
        texts.append(to_text(html, subject=row.subject, sender=row.sender_email))

    # Fresh emails are classified by the model, one batch. Rule, trusted and
    # duplicate emails are answered without spending a token.
    model_verdicts = await classify_many(texts)

    by_id = {row.id: verdict for row, verdict in zip(fresh_rows, model_verdicts, strict=True)}
    for row, rule in rule_rows:
        by_id[row.id] = _rule_verdict(rule)
    for row in trusted_rows:
        by_id[row.id] = _trusted_verdict()
    for row in duplicate_rows:
        by_id[row.id] = _reused_verdict(hash_verdicts[row.body_hash])

    async with tenant_session(tenant) as session:
        for row in rows:
            verdict = by_id[row.id]
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
            await _record_gate_usage(
                session,
                tenant=tenant,
                email_message_id=row.id,
                verdict=verdict,
            )
            # Seed trust only after a verdict the gate actually answered (see
            # the single-email path for why `uncertain` is excluded).
            if verdict.status == "recruitment" and verdict.model != TRUSTED_SENDER_MODEL:
                await _mark_trusted(session, tenant, row.sender_email)

    # Enqueued after the writes commit, and only then: a job that started
    # before the status moved could read the row mid-flight, and one enqueued
    # for a row whose write failed would extract from an unclassified email.
    for row in rows:
        verdict = by_id[row.id]
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
    from app.services.ingest.forwarding import extract_original_sender
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
    if not settings.llm_provider_configured(settings.EXTRACTION_MODEL_FAST):
        log.error(
            "llm_not_configured",
            job="extract_email",
            detail=(
                "Set LLM_PROVIDER_BASE_URL, LLM_PROVIDER_API_KEY and EXTRACTION_MODEL_FAST."
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

    # Claim the row so a second job reading the same `classified` email bows
    # out instead of paying for the same extraction. Only a `classified` row
    # can be claimed; a row already at `extracting` is a rescan recovery (a
    # worker died mid-call and the sweep re-enqueued it), so that path proceeds
    # without a claim — the deterministic opportunity ids make a re-run safe.
    if row.processing_status == "classified":
        async with tenant_session(tenant) as session:
            claimed = (
                await session.execute(_CLAIM_EXTRACTING, {"id": email_message_id})
            ).one_or_none()
        if claimed is None:
            log.info(
                "extract_skipped_already_claimed",
                email_message_id=email_message_id,
            )
            return
    else:
        # The recovery path — the row is already `extracting`, so this pickup
        # is a crash-loop iteration by definition (forward progress never
        # re-enters `extracting`). Spend the budget BEFORE the model call;
        # past the ceiling the row is parked `failed` and the sweep stops
        # seeing it. This is the bound that ends what was an infinite
        # rebill loop on any unexpected exception in the code below.
        if await _llm_attempts_exhausted(tenant, email_message_id):
            log.warning(
                "extract_attempts_exhausted",
                email_message_id=email_message_id,
            )
            await _fail_extraction(tenant, email_message_id, "extraction attempts exhausted")
            return

    html = await body_store().get(row.body_html_r2_key) or ""
    # `to_text` is the single source of truth for the text the model's offsets
    # index into. Extracting from anything else — the raw HTML, the preview —
    # would make every span the model returns fail verification.
    source = to_text(html, subject=row.subject, sender=row.sender_email)

    # Truncate the tail before extraction. A job order states everything in
    # its first screen (the Etiqa sample is ~800 chars); a long reply chain
    # is mostly quoted history the model does not need. Truncation happens
    # AFTER `to_text` so the evidence spans the model returns are checked
    # against the same string it saw — cutting earlier (raw HTML) would make
    # offsets drift from what was actually sent.
    if len(source) > settings.EXTRACTION_MAX_CHARS:
        source = source[: settings.EXTRACTION_MAX_CHARS]

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

    # The forwarding header is in the body text that `to_text` preserved.
    # Parsing it is cheap and deterministic — no model call — and it is what
    # tells us the original sender (who has the client relationship) apart
    # from the forwarder (who just relayed the mail).
    original_sender = extract_original_sender(source)
    ids = await persist(
        tenant, uuid.UUID(email_message_id), response, result, source,
        original_sender_email=original_sender.email if original_sender else None,
        original_sender_name=original_sender.name if original_sender else None,
    )
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


async def replay_email(
    ctx, *, email_message_id: str, tenant_id: str, mailbox_id: str
) -> None:
    """Re-read one already-extracted email under the current prompt.

    The deliberate half of extraction: `replay_stale_extractions` finds emails
    whose latest extraction ran under an older prompt version, claims them to
    `replaying`, and enqueues this job. `persist(replay=True)` refreshes the
    extraction-derived columns of the existing opportunity rows — the whole
    point of replaying is that a newer prompt answers better (see
    `_REPLAYABLE` in persist.py for what may and may not change).

    Deliberately NOT the same job as `extract_email`, even though the body is
    nearly identical. The difference is the one flag: `extract_email` runs
    `persist(replay=False)`, so a retry of it can never accidentally refresh —
    and refresh is exactly what a crash-recovered replay must do, which is why
    `RESUME_JOB` maps `replaying` here and not to `extract_email`. Sharing the
    body through a flag would let one misplaced call erase that separation.
    """
    from app.services.ingest.extract import extract
    from app.services.ingest.forwarding import extract_original_sender
    from app.services.ingest.persist import persist
    from app.services.ingest.preprocess import to_text
    from app.services.llm.client import LLMInvalidJSON

    if not settings.llm_provider_configured(settings.EXTRACTION_MODEL_FAST):
        log.error(
            "llm_not_configured",
            job="replay_email",
            detail=(
                "Set LLM_PROVIDER_BASE_URL, LLM_PROVIDER_API_KEY and EXTRACTION_MODEL_FAST."
            ),
        )
        raise RuntimeError("Extraction has no model configured.")

    tenant = uuid.UUID(tenant_id)
    mailbox = uuid.UUID(mailbox_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                _REPLAY_CLAIM, {"id": email_message_id, "mailbox_id": mailbox}
            )
        ).one_or_none()

    if row is None:
        # Unknown row, a tenant that does not own it, or a claim the sweep never
        # made — RLS already decided the first two, and the status guard decided
        # the third. Nothing to do and nothing to report.
        log.info("replay_skipped_unknown_row", email_message_id=email_message_id)
        return
    if row.processing_status != "replaying":
        log.info(
            "replay_skipped_not_claimed",
            email_message_id=email_message_id,
            status=row.processing_status,
        )
        return
    # Every replay pickup is deliberate spend — the claim resolver moved the
    # row to `replaying` before this job first saw it, so first-vs-recovery is
    # not distinguishable and both count. Three lifetime replays per email is
    # the bound; a prompt-version sweep that finds the row again after that
    # leaves it `failed` for a human rather than re-billing it forever.
    if await _llm_attempts_exhausted(tenant, email_message_id):
        log.warning(
            "replay_attempts_exhausted",
            email_message_id=email_message_id,
        )
        await _fail_extraction(tenant, email_message_id, "replay attempts exhausted")
        return

    html = await body_store().get(row.body_html_r2_key) or ""
    source = to_text(html, subject=row.subject, sender=row.sender_email)
    if len(source) > settings.EXTRACTION_MAX_CHARS:
        source = source[: settings.EXTRACTION_MAX_CHARS]

    try:
        response, result = await extract(source)
    except LLMInvalidJSON as exc:
        # Same discipline as `extract_email`: both models were asked and neither
        # answered in the required shape. The email is left for a human to look
        # at rather than retried on the same tokens.
        log.warning("replay_failed", email_message_id=email_message_id, error=str(exc))
        await _fail_extraction(tenant, email_message_id, str(exc))
        return

    original_sender = extract_original_sender(source)
    ids = await persist(
        tenant, uuid.UUID(email_message_id), response, result, source,
        original_sender_email=original_sender.email if original_sender else None,
        original_sender_name=original_sender.name if original_sender else None,
        replay=True,
    )
    status = "extracted" if ids else "no_opportunity"

    # Success: the pickup this run spent was a legitimate one, so the budget
    # hands it back. `llm_attempts` counts failed pickups only — a healthy
    # email replays once per prompt upgrade forever; only a row whose replays
    # keep crashing burns toward the ceiling and parks `failed`.
    async with tenant_session(tenant) as session:
        await session.execute(_RESET_LLM_ATTEMPTS, {"id": email_message_id})
        await session.execute(
            _FINISH_EXTRACTION, {"status": status, "id": email_message_id}
        )

    log.info(
        "replay_recorded",
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


async def _record_gate_usage(
    session, *, tenant: uuid.UUID, email_message_id: uuid.UUID, verdict
) -> None:
    """Persist one gate-usage row inside the caller's transaction.

    The gate is the only LLM call in the system with no cost provenance, and
    cost planning is guesswork without it. A verdict that billed tokens but
    answered nothing (`uncertain` from a transport error) still records its
    token counts — the money was spent even though the email proceeds to
    extraction. A failure to record is logged and swallowed rather than
    propagated: losing the verdict over a telemetry row would be worse than
    losing the telemetry.
    """
    try:
        from app.services.ingest.usage import record_classification_usage

        await record_classification_usage(
            session,
            tenant_id=tenant,
            email_message_id=email_message_id,
            model_name=verdict.model,
            prompt_tokens=verdict.prompt_tokens,
            completion_tokens=verdict.completion_tokens,
            latency_ms=verdict.latency_ms,
        )
    except Exception as exc:
        log.warning(
            "gate_usage_record_failed",
            email_message_id=str(email_message_id),
            error=repr(exc),
        )


def _trusted_verdict():
    """The verdict a trusted sender earns without a model call.

    Zero tokens, zero latency — the whole point. `model` is
    `TRUSTED_SENDER_MODEL` so reports and the seed logic can distinguish it.
    """
    from app.services.ingest.classify import Classification

    return Classification(
        status="recruitment",
        reason="sender domain trusted by earlier verdicts",
        model=TRUSTED_SENDER_MODEL,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
    )


def _rule_verdict(rule: tuple[str, str]):
    """The verdict a deterministic noise rule earns without a model call.

    `rule` is `(status, reason)` from `gate_rules.gate_rule`. Only
    `non_recruitment` ever reaches here (the rules never answer
    `recruitment`), so a rule verdict can only skip extraction — it can never
    skip a job order. `model` is `gate-rule` so the cost report can count the
    free answers.
    """
    from app.services.ingest.classify import Classification

    status, reason = rule
    return Classification(
        status=status,
        reason=reason,
        model=GATE_RULE_MODEL,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
    )


def _reused_verdict(prior) -> "Classification":  # noqa: F821 — type only, imported lazily
    """The verdict an identical body earns from an earlier email's gate run.

    `prior` is one row of `_FIND_PRIOR_VERDICT`. The verdict is reused
    verbatim — including its `classification_model`, which is why a reused
    verdict never re-seeds trust (the seed check requires a model other than
    `TRUSTED_SENDER_MODEL`, and a reused verdict carries the model of whoever
    answered first). Zero tokens: the money was spent on the first copy.
    """
    from app.services.ingest.classify import Classification

    return Classification(
        status=prior.classification_status,
        reason=(
            "body identical to an earlier email: "
            + (prior.classification_reason or "")
        ),
        model=prior.classification_model,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
    )


async def _is_trusted(session, tenant: uuid.UUID, sender_email: str | None) -> bool:
    """Fail-open trust check: any error reads as not trusted (gate runs)."""
    try:
        from app.services.ingest.sender_trust import is_trusted_domain

        return await is_trusted_domain(session, tenant_id=tenant, sender_email=sender_email)
    except Exception as exc:
        log.warning(
            "trusted_sender_check_failed",
            tenant_id=str(tenant),
            error=repr(exc),
        )
        return False


async def _mark_trusted(session, tenant: uuid.UUID, sender_email: str | None) -> None:
    """Idempotent trust seed. Failures are logged and swallowed: a lost trust
    row costs a future gate call, never a dropped job order."""
    try:
        from app.services.ingest.sender_trust import mark_trusted_domain

        await mark_trusted_domain(session, tenant_id=tenant, sender_email=sender_email)
    except Exception as exc:
        log.warning(
            "trusted_sender_seed_failed",
            tenant_id=str(tenant),
            error=repr(exc),
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
    except TokenRefreshTransientError as exc:
        # A throttled or slow Entra, not a dead grant: deferring is the whole
        # fix, and the mailbox keeps its current subscription until the retry.
        raise Retry(defer=settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS) from exc

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
    except TokenRefreshTransientError as exc:
        # Graph asked us to prove the grant, and Entra is throttling the
        # proof. Not a dead grant: defer and prove it again.
        raise Retry(defer=settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS) from exc

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
    except TokenRefreshTransientError as exc:
        # A throttled or slow Entra, not a dead grant. Defer; the backfill is
        # still due and the mailbox is not flagged for a reconnect.
        raise Retry(defer=settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS) from exc

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

    async with tenant_session(tenant) as session:
        paused = (
            await session.execute(_INGEST_PAUSED, {"mailbox_id": mailbox})
        ).scalar_one_or_none()
    if paused is not None:
        # Intake is paused. `active_mailboxes()` already excludes this mailbox
        # from the scheduled sweep, but this job is also enqueued directly — a
        # `missed` lifecycle event, `recreate_subscription`'s follow-up — so
        # the gate lives at the job, where every caller funnels through.
        # Walking anyway would advance nothing the resume walk does not
        # rebuild, and would ingest exactly the window the pause exists to
        # skip.
        log.info("delta_sync_skipped_intake_paused", mailbox_id=mailbox_id)
        return

    try:
        client = await graph_client_for_mailbox(tenant, mailbox)
    except MailboxNotAuthorised as exc:
        await mark_needs_reauth(tenant, mailbox, str(exc))
        return
    except TokenRefreshTransientError as exc:
        # Entra throttled or was slow — the grant is fine, so this is not a
        # reconnect and it is not a failure worth a row on the panel. The
        # scheduled sweep re-enqueues this job on its own clock anyway, and
        # arq's own retry covers the gap; a transient blip must not read as
        # "checking your mailbox failed".
        log.info("delta_sync_refresh_transient", mailbox_id=mailbox_id)
        raise Retry(defer=settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS) from exc

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
    # The hash is of the exact text the gate and extractor will read — the
    # same `to_text` call they make, so two emails that flatten to the same
    # text get the same hash and the classify dedupe fires. Computing it here
    # (once, at fetch) rather than in each classify/extract job is what keeps
    # every later reader from re-doing the HTML parse.
    from app.services.ingest.preprocess import to_text

    body_text = to_text(html, subject=message.get("subject"), sender=sender.get("address"))
    body_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()

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
                "body_hash": body_hash,
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
    # After the commit, and before the sync event below so the dashboard is
    # already refetching while that row is written. This is the one failure the
    # user can fix themselves, and until they do nothing else arrives — so it
    # is precisely the state that must not wait for someone to reload the page
    # out of curiosity about a quiet week.
    await publish(tenant_id, KIND_MAILBOX)
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


