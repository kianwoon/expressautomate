"""Write an extraction and its opportunities in one transaction (plan §14).

Append-only with respect to history: every run inserts a new `extractions` row.
Nothing is updated in place, so an email's extraction history is the ordered
set of its rows.

Opportunities are the exception, and the asymmetry is deliberate but narrow.
Their ids are derived deterministically from the email and the job's position
within the extraction, so a *retry* — `rescan_stuck` re-running a job that died
between this transaction and `_FINISH_EXTRACTION` — produces the same ids and
inserts nothing the second time. Without that, the retry minted fresh ids, the
notification dedupe index never fired, and the recruiter was told twice about
one vacancy.

The cost is that a deliberate *replay* under a better prompt is currently a
no-op for opportunities: the new `extractions` row lands with its evidence, but
the improved field values are discarded by the same `ON CONFLICT DO NOTHING`
that makes retries safe. Nothing distinguishes the two cases today because
nothing replays yet. Whoever builds replay must separate them — most likely by
keying the id on the extraction rather than the email — and should not simply
drop the conflict clause, which would restore the duplicate-notification bug.

Positional keying carries a second caveat worth knowing before replay exists:
it assumes the model returns the same jobs in the same order for the same
email. That holds at temperature zero and does not hold across a prompt or
model change, where job 2 of the new run may be a different vacancy from job 2
of the old one.

Human corrections live in `opportunity_field_overrides` and are never read or
written here. That separation is what makes replay safe: this module physically
cannot clobber a recruiter's fix, because it never issues an UPDATE against
anything a human has touched.
"""

import json
import uuid

from sqlalchemy import ARRAY, Text, bindparam, text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.services.client_matching import match_client
from app.services.events import KIND_EXTRACTION, publish
from app.services.ingest.evidence import parse_salary, quality_state, verify
from app.services.ingest.glossary import DetectedCode, GlossaryEntry, detect
from app.services.ingest.schema import ExtractedField, ExtractedJob, ExtractionResponse
from app.services.llm.client import LLMResult
from app.services.notify.dispatch import emit, enqueue_deliveries
from app.services.notify.events import (
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_NEW,
    OpportunityEvent,
)

log = get_logger(__name__)

# Model field name -> the `opportunities` column that holds its raw string.
# allow-hardcode: the target columns of a table, not configuration. A name here
# that no column matches is a defect, so this map moves with the migration.
_SIMPLE = {
    "company": "company_name_raw",
    "job_title": "job_title_raw",
    "job_description": "job_description",
    "requirements": "requirements",
    "working_hours": "working_hours_raw",
    "work_arrangement": "work_arrangement",
    "employment_type": "employment_type",
    "duration": "duration_raw",
    "location": "location_raw",
}

_INSERT_EXTRACTION = text(
    """
    INSERT INTO extractions
        (id, tenant_id, email_message_id, model_name, prompt_version,
         prompt_tokens, completion_tokens, latency_ms, raw_response)
    VALUES (:id, :tenant_id, :email_message_id, :model_name, :prompt_version,
            :prompt_tokens, :completion_tokens, :latency_ms,
            CAST(:raw_response AS jsonb))
    """
)

# INSERT ... SELECT, so `received_datetime` is denormalised from the email in
# the same statement rather than round-tripped first. The SELECT reads under
# the tenant policy, so an email another tenant owns yields no row and inserts
# no opportunity — the mismatch fails closed instead of writing a headless row.
#
# ON CONFLICT (id) DO NOTHING: `:id` is now a deterministic uuid5 (see
# `_opportunity_id` below), so a retried extraction — `rescan_stuck` re-running
# `extract_email` after a worker died between `persist()`'s commit and
# `_FINISH_EXTRACTION` — computes the SAME id for the same vacancy and lands
# here a second time. Without the clause that would be a primary-key
# violation; with it, the row from the first run is left exactly as it was,
# including any human correction recorded against it, and the retry's
# evidence/codes rows (below) still attach to the id that already exists.
_INSERT_OPPORTUNITY = text(
    f"""
    INSERT INTO opportunities
        (id, tenant_id, email_message_id, received_datetime,
         {", ".join(_SIMPLE.values())},
         salary_min, salary_max, salary_currency, salary_period, salary_raw,
         skills, quality_state, review_status)
    SELECT :id, :tenant_id, :email_message_id, em.received_datetime,
           {", ".join(f":{name}" for name in _SIMPLE)},
           :salary_min, :salary_max, :salary_currency, :salary_period, :salary_raw,
           :skills, :quality_state, :review_status
    FROM email_messages em WHERE em.id = :email_message_id
    ON CONFLICT (id) DO NOTHING
    """
).bindparams(bindparam("skills", type_=ARRAY(Text)))

# Fixed namespace for uuid5, in the same style as
# `app.api.auth.PERSONAL_TENANT_NAMESPACE`: a constant, not a secret, so it can
# live in source. Deriving from (email_message_id, index-within-the-run)
# rather than from the model's output means a retry that gets a byte-for-byte
# identical answer (the common case — extraction runs at temperature zero)
# reproduces the same id, which is what lets the dedupe above and the
# notification dedupe index (`notification_deliveries`'s partial unique index
# on (destination_id, event_kind, subject_id)) both actually fire on a retry.
# A retry whose answer differs (a prompt/model upgrade re-run over old mail)
# still lands on the same id for the same position — that is intentional: the
# opportunity that email describes is one thing across replays, not a new one
# each time the model is asked again.
_OPPORTUNITY_ID_NAMESPACE = uuid.UUID("2f6b6e4a-8a3d-5b4a-9c1e-6a2d4e8f1b7c")


def _opportunity_id(email_message_id: uuid.UUID, index: int) -> uuid.UUID:
    return uuid.uuid5(_OPPORTUNITY_ID_NAMESPACE, f"{email_message_id}:{index}")

_INSERT_EVIDENCE = text(
    """
    INSERT INTO extraction_evidence
        (id, tenant_id, extraction_id, opportunity_id, field_name,
         extracted_value, evidence_text, start_char, end_char,
         model_confidence, evidence_valid)
    VALUES (:id, :tenant_id, :extraction_id, :opportunity_id, :field_name,
            :extracted_value, :evidence_text, :start_char, :end_char,
            :model_confidence, :evidence_valid)
    """
)


# Read under the tenant policy like everything else, so a scan can only ever
# use the glossary of the agency whose email is being read. `code` is the
# operator's own spelling — the scanner needs that, not the normalised form,
# because a pattern built from the folded text would look for `cf` and match
# the letters of an ordinary word.
_SELECT_GLOSSARY = text(
    "SELECT code, meaning, attribute FROM glossary_codes ORDER BY code"
)

# The matcher needs the sender, which lives on the message rather than in the
# extraction. Read inside the same transaction so it cannot disagree with what
# the rest of this write assumes.
_SENDER = text("SELECT sender_email FROM email_messages WHERE id = :id")

_INSERT_CODE = text(
    """
    INSERT INTO opportunity_codes
        (id, tenant_id, opportunity_id, code, meaning, attribute,
         start_char, end_char)
    VALUES (:id, :tenant_id, :opportunity_id, :code, :meaning, :attribute,
            :start_char, :end_char)
    """
)


# The vocabulary `salary_period` may hold. `week` is canonical even though the
# prompt in extract.py does not ask for it, because emails say "weekly" and the
# salary sort already knows how to weigh one. Adding a period here means adding
# it to the CHECK constraint and to the sort's per-period factors in the same
# breath, which is why the vocabulary lives in code beside them rather than in
# settings — it is the shape of the data, not a setting anyone tunes.
_SALARY_PERIODS = ("hour", "day", "week", "month", "year")

# The words that do not contain the period they mean, so no amount of matching
# on the vocabulary above will find them.
#
# allow-hardcode: this is English irregularity, not a detect-list — "daily" is
# not spelled with "day" in it and "per annum" is not spelled with "year" in
# it. Everything regular ("monthly", "hourly", "per week") is derived from
# `_SALARY_PERIODS` rather than listed, so this stays short by construction
# instead of growing a line per phrasing a model happens to emit.
#
# Stems, not whole words, so one entry covers "annual", "annually", "annum"
# and "per annum". Both are long enough not to appear inside an unrelated
# word — which is why "p.a." is deliberately absent: a two-letter stem would
# match far more than it should, and an unread period is NULL, not a guess.
_IRREGULAR_PERIODS = {"dail": "day", "annu": "year"}


def _salary_period(field: ExtractedField | None) -> str | None:
    """The period as one of a known few, or nothing at all.

    A model asked for "month" will sometimes answer "Monthly" or "per month",
    and storing that verbatim is what made the salary sort drop those rows: it
    looked the period up in a table keyed on the canonical word and found
    nothing. Normalising here means the column holds one spelling of each
    period, which is what lets a CHECK constraint state that as a rule.

    An unrecognised word becomes NULL rather than a guess. That is the §15
    line — "the email said something we could not read" is not the same as
    "the email said monthly" — and nothing is lost by admitting it: the
    sentence the model read still sits in `salary_raw` and on the evidence row.
    """
    raw = _value(field)
    if raw is None:
        return None
    # Letters only, so "p.a.", "per-month" and "  Monthly " all reduce to the
    # word underneath the punctuation the model chose to decorate it with.
    cleaned = "".join(char for char in raw.lower() if char.isalpha())
    if not cleaned:
        return None
    for period in _SALARY_PERIODS:
        if period in cleaned:
            return period
    for irregular, period in _IRREGULAR_PERIODS.items():
        if irregular in cleaned:
            return period
    return None


def _value(field: ExtractedField | None) -> str | None:
    """`Not mentioned` becomes NULL in the column.

    The raw string still lives on the evidence row, so "the model was asked and
    said the email does not state this" stays distinguishable from "the model
    never answered for this field at all" (plan §15).
    """
    if field is None or field.is_missing:
        return None
    return field.value


async def persist(
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID,
    response: ExtractionResponse,
    result: LLMResult,
    source: str,
) -> list[uuid.UUID]:
    """Record one model run and every vacancy it found. Returns the new ids.

    One transaction for the whole run. A partial write — the extraction row
    without its evidence, or two of three vacancies — would look like a
    complete answer to everything downstream, and there is nothing in the data
    that could later tell it apart from one.
    """
    extraction_id = uuid.uuid4()
    opportunity_ids: list[uuid.UUID] = []
    # Every id emit() writes back, pending and rate-capped alike —
    # enqueue_deliveries() re-reads each row's own status afterwards, so this
    # list does not need to filter anything itself (see dispatch.py).
    delivery_ids: list[uuid.UUID] = []

    async with tenant_session(tenant_id) as session:
        await session.execute(
            _INSERT_EXTRACTION,
            {
                "id": extraction_id,
                "tenant_id": tenant_id,
                "email_message_id": email_message_id,
                "model_name": result.model,
                "prompt_version": settings.PROMPT_VERSION,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "latency_ms": result.latency_ms,
                "raw_response": json.dumps(result.raw),
            },
        )

        # Scanned once for the whole email, inside this transaction. Codes are
        # part of what a job order means, so a commit that stored the vacancy
        # and lost its shorthand would leave a row that reads as though the
        # client stated no requirement at all.
        codes = detect(source, await _glossary(session))

        # One client per email, not per vacancy: three vacancies in one mail
        # come from one company, and proposing three identical clients would
        # make the review queue unusable on the first busy day.
        sender_email = (
            await session.execute(_SENDER, {"id": email_message_id})
        ).scalar_one_or_none()
        first_company = next(
            (_value(job.company) for job in response.jobs if _value(job.company)),
            None,
        )
        await match_client(session, tenant_id, email_message_id, sender_email, first_company)

        # An email describing three vacancies becomes three rows. They share
        # one extraction, because they came from one model call — that is what
        # makes "what did this run cost, and what did it produce" answerable.
        for index, job in enumerate(response.jobs):
            opportunity_id = _opportunity_id(email_message_id, index)
            opportunity_ids.append(opportunity_id)
            await _insert_opportunity(
                session, tenant_id, email_message_id, opportunity_id, job, source
            )
            await _insert_evidence(
                session, tenant_id, extraction_id, opportunity_id, job, source
            )
            await _insert_codes(
                session, tenant_id, opportunity_id, job, codes, len(response.jobs)
            )

            # Inside the same transaction that created the opportunity, so a
            # notification for a job order that rolled back can never exist —
            # that half of the "either both commit or neither does" reasoning
            # holds. The reverse does not: the notification path is new and
            # far less exercised than ingestion, and a subscriber lookup or
            # rate-cap query failing inside emit() must not be allowed to take
            # the extraction down with it. An opportunity that is retained is
            # still visible in the dashboard; one that rolled back over a
            # notification bug is gone until the email is reprocessed. So the
            # isolation is asymmetric on purpose: wrap emit() in a SAVEPOINT
            # (begin_nested) rather than let it share the outer transaction
            # unguarded. If it raises, roll back only its own writes — the
            # extraction and opportunity rows already staged in the outer
            # transaction are untouched — and continue. Without the savepoint,
            # a raised exception poisons the whole Postgres transaction (every
            # later statement fails with InFailedSqlTransactionError) even if
            # the exception itself were caught, so the savepoint is what keeps
            # the session usable afterwards, not just the try/except.
            state = quality_state(job, source)
            try:
                async with session.begin_nested():
                    delivery_ids.extend(
                        await emit(
                            OpportunityEvent(
                                kind=(
                                    EVENT_OPPORTUNITY_NEEDS_REVIEW
                                    if state == "needs_review"
                                    else EVENT_OPPORTUNITY_NEW
                                ),
                                tenant_id=tenant_id,
                                opportunity_id=opportunity_id,
                                # Raw, not normalised: this is what a
                                # recruiter recognises, and the message is
                                # read by a person.
                                job_title=_value(job.job_title),
                                company_name=_value(job.company),
                                location=_value(job.location),
                                salary=_value(job.salary),
                            ),
                            session,
                        )
                    )
            except Exception:
                # Logged, not raised: a lost notification must be visible to
                # an operator, but it must never be the reason a valid
                # extraction disappears. Anything emit() partially wrote is
                # already gone — the savepoint rolled it back — so nothing
                # here is added to delivery_ids and enqueue_deliveries() will
                # never be asked about ids that do not exist.
                log.exception(
                    "notify_emit_failed",
                    tenant_id=str(tenant_id),
                    opportunity_id=str(opportunity_id),
                    extraction_id=str(extraction_id),
                )

    # Outside the transaction, deliberately. Redis cannot join it, and a job
    # that starts before its row is committed reads nothing and exits without
    # retrying. `enqueue` fails soft; `flush_notifications` is what turns a
    # lost job back into a queued one.
    await enqueue_deliveries(tenant_id, delivery_ids)

    # Same reasoning, for the dashboard rather than the queue: the transaction
    # above has committed, so a browser that refetches on this nudge sees the
    # extraction and its vacancies. Sent even when the email described no
    # vacancy — the email's own status moved to `no_opportunity`, which is
    # visible on the dashboard and is the answer someone watching is waiting
    # for.
    await publish(tenant_id, KIND_EXTRACTION)

    return opportunity_ids


async def _glossary(session) -> list[GlossaryEntry]:
    """This tenant's shorthand, or nothing.

    The read goes through the same tenant-scoped session as the writes, so an
    agency's dictionary can only ever be applied to that agency's mail — the
    failure mode of a mis-scoped session is an empty glossary and no decoding,
    never another agency's definitions attached to this client's words.
    """
    rows = (await session.execute(_SELECT_GLOSSARY)).all()
    return [GlossaryEntry(code=r.code, meaning=r.meaning, attribute=r.attribute) for r in rows]


def _covers(job: ExtractedJob, code: DetectedCode) -> bool:
    """Does this vacancy's evidence surround where the code was found?

    Only asked when one email described several vacancies. The codes are
    located in the email, not in a vacancy, so attaching all of them to all of
    them would tell a recruiter that the client's requirement for the second
    role applies to the third — a demographic requirement invented by
    bookkeeping, which is the exact failure this feature must not have.

    The span of a vacancy's verified evidence is the best available answer to
    "which part of the email is this row about", and it is a deterministic one.
    A vacancy whose evidence located nowhere claims no part of the email and so
    claims no codes: silence is the honest output when the boundary is unknown.
    """
    spans = [
        (f.start_char, f.end_char)
        for f in vars(job).values()
        if isinstance(f, ExtractedField)
        and f.start_char is not None
        and f.end_char is not None
    ]
    if not spans:
        return False
    return min(s for s, _ in spans) <= code.start_char and code.end_char <= max(
        e for _, e in spans
    )


async def _insert_codes(
    session,
    tenant_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    job: ExtractedJob,
    codes: list[DetectedCode],
    job_count: int,
) -> None:
    """Attach the decoded shorthand, with the meaning copied onto the row.

    `meaning` and `attribute` are written as values, never referenced. An
    agency that later corrects its glossary corrects what happens next; what a
    recruiter was told in January stays on the January row, because otherwise
    the audit trail rewrites itself and stops being evidence of anything.

    A single-vacancy email hands every code to that vacancy — there is nowhere
    else for them to belong. Beyond one, `_covers` decides.
    """
    for code in codes:
        if job_count > 1 and not _covers(job, code):
            continue
        await session.execute(
            _INSERT_CODE,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "opportunity_id": opportunity_id,
                "code": code.code,
                "meaning": code.meaning,
                "attribute": code.attribute,
                "start_char": code.start_char,
                "end_char": code.end_char,
            },
        )


async def _insert_opportunity(
    session,
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    job: ExtractedJob,
    source: str,
) -> None:
    salary_min = salary_max = currency = None
    if job.salary is not None and not job.salary.is_missing:
        salary_min, salary_max, currency = parse_salary(job.salary.value)

    state = quality_state(job, source)
    params = {
        "id": opportunity_id,
        "tenant_id": tenant_id,
        "email_message_id": email_message_id,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": currency,
        "salary_period": _salary_period(job.salary_period),
        "salary_raw": _value(job.salary),
        "skills": _skills(job),
        "quality_state": state,
        # Deterministic checks decide review, not the model's opinion of
        # itself. `likely` is deliberately not routed to a human: it means
        # every span checked out and something softer is missing, which is a
        # usable row, and queueing those would bury the ones that are wrong.
        "review_status": "needs_review" if state == "needs_review" else "ready",
    }
    params.update({name: _value(getattr(job, name)) for name in _SIMPLE})
    await session.execute(_INSERT_OPPORTUNITY, params)


def _skills(job: ExtractedJob) -> list[str]:
    """The model returns one string; the column is an array.

    Split rather than stored whole so a skill is queryable on its own. Empty
    rather than NULL when nothing was extracted: an absent skill list and an
    empty one mean the same thing to every query that would read it.
    """
    raw = _value(job.skills) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


async def _insert_evidence(
    session,
    tenant_id: uuid.UUID,
    extraction_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    job: ExtractedJob,
    source: str,
) -> None:
    """One row per field the model answered for, valid or not.

    The invalid ones are the point. Dropping a span that does not exist would
    erase the only record that the model claimed something the source does not
    say, and that record is what makes a prompt regression visible.
    """
    for name, field in vars(job).items():
        if not isinstance(field, ExtractedField):
            continue
        # Before the offsets are read, not inline with them: `verify` writes
        # the offsets it located back onto the field, and a dict literal
        # evaluates its values in order — reading `start_char` first would
        # store the model's arithmetic and then discover it was wrong.
        valid = verify(field, source)
        await session.execute(
            _INSERT_EVIDENCE,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "extraction_id": extraction_id,
                "opportunity_id": opportunity_id,
                "field_name": name,
                "extracted_value": field.value,
                "evidence_text": field.evidence,
                "start_char": field.start_char,
                "end_char": field.end_char,
                "model_confidence": field.confidence,
                "evidence_valid": valid,
            },
        )
