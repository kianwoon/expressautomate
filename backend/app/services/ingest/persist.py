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

The cost used to be that a deliberate *replay* under a better prompt was a no-op
for opportunities: the new `extractions` row landed with its evidence, but the
improved field values were discarded by the same `ON CONFLICT DO NOTHING` that
makes retries safe. Replay now exists — `replay_stale_extractions` re-reads
emails whose latest extraction ran under an older prompt — and it is separated
from retry by an explicit `replay=True` on `persist`, which refreshes only the
extraction-derived columns (`_REPLAYABLE`) and never the columns a person or the
pipeline decided (`assigned_user_id`, `client_id`, `opportunity_field_overrides`
corrections, a `reviewed` sign-off, a lawful `sex_requirement`). The conflict
clause was never dropped, so the duplicate-notification bug the clause exists to
prevent stays prevented: a replay notifies nobody, because a vacancy that
already exists is not new.

Positional keying carries a second caveat worth knowing before replay exists:
it assumes the model returns the same jobs in the same order for the same
email. That holds at temperature zero and does not hold across a prompt or
model change, where job 2 of the new run may be a different vacancy from job 2
of the old one. The replay UPDATE therefore refreshes the row the id names and
leaves identity matching to the review queue — a row whose title/company/location
changed under the new prompt is a different vacancy and the reviewer sees both
the old and new values in the audit trail.

One deliberate UPDATE exists: `_maybe_supersede` points an older open
opportunity at the row that just replaced it when a later email *changes* the
job order's requirements (sex, race, salary...). It writes only the two
supersede columns — never an extracted field, and never a human correction,
which live in `opportunity_field_overrides` and are not read or written here.
That separation is what keeps replay safe: this module physically cannot
clobber a recruiter's fix, because it never issues an UPDATE against anything
a human has touched.
"""

import json
import uuid

from sqlalchemy import ARRAY, Text, bindparam, func, select, text, update
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models import EmailMessage, Opportunity
from app.services.client_matching import match_client
from app.services.events import KIND_EXTRACTION, publish
from app.services.ingest.evidence import (
    parse_salary,
    parse_salary_period,
    quality_state,
    salary_currency,
    verify,
)
from app.services.ingest.glossary import DetectedCode, GlossaryEntry, detect
from app.services.ingest.schema import ExtractedField, ExtractedJob, ExtractionResponse
from app.services.llm.client import LLMResult
from app.services.notify.dispatch import emit, enqueue_deliveries
from app.services.notify.events import (
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_NEW,
    OpportunityEvent,
)
from app.services.sourcing.preference import implied_sex

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
         skills, quality_state, review_status, client_id, assigned_user_id,
         sex_requirement, sex_requirement_reason)
    SELECT :id, :tenant_id, :email_message_id, em.received_datetime,
           {", ".join(f":{name}" for name in _SIMPLE)},
           :salary_min, :salary_max, :salary_currency, :salary_period, :salary_raw,
           :skills, :quality_state, :review_status, :client_id, :assigned_user_id,
           :sex_requirement, :sex_requirement_reason
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
_SELECT_GLOSSARY = text("SELECT code, meaning, attribute FROM glossary_codes ORDER BY code")

# The columns a deliberate replay may refresh, keyed by the model field they
# come from. Deliberately NOT every column the INSERT writes: `client_id` and
# `assigned_user_id` are matched/claimed once and may have been corrected by a
# person since, `received_datetime` is denormalised from the email (unchanged),
# and `superseded_by_opportunity_id`/`placement_type` are lifecycle state. A
# replay refreshes what the extraction produced; it never re-decides what a
# human or the pipeline already decided.
# allow-hardcode: the target columns of a table, not configuration.
_REPLAYABLE = {
    "company_name_raw",
    "job_title_raw",
    "job_description",
    "requirements",
    "working_hours_raw",
    "work_arrangement",
    "employment_type",
    "duration_raw",
    "location_raw",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "salary_raw",
    "skills",
    "quality_state",
    "review_status",
    "sex_requirement",
    "sex_requirement_reason",
}

# Which columns a human may have corrected, and that replay must therefore not
# overwrite. `field_name` on `opportunity_field_overrides` is the DB column name
# (the same vocabulary `_SIMPLE` maps to), so the check is a plain membership
# test against the columns replay would otherwise write.
_SELECT_OVERRIDES = text(
    "SELECT field_name FROM opportunity_field_overrides WHERE opportunity_id = :id"
)

# The matcher needs the sender, which lives on the message rather than in the
# extraction. Read inside the same transaction so it cannot disagree with what
# the rest of this write assumes. The LEFT JOIN to mailboxes picks up the
# recruiter who owns the mailbox — the person the client emailed to, who
# becomes a newly-created client's assigned recruiter — without losing the
# sender when the mailbox row has gone (a deleted mailbox nulls user_id but
# the sender_email still reaches the matcher).
_SENDER = text(
    """
    SELECT em.sender_email, em.sender_name, m.user_id AS mailbox_owner_id
    FROM email_messages em
    LEFT JOIN mailboxes m ON em.mailbox_id = m.id
    WHERE em.id = :id
    """
)

_INSERT_CODE = text(
    """
    INSERT INTO opportunity_codes
        (id, tenant_id, opportunity_id, code, meaning, attribute,
         start_char, end_char)
    VALUES (:id, :tenant_id, :opportunity_id, :code, :meaning, :attribute,
            :start_char, :end_char)
    ON CONFLICT (opportunity_id, code, start_char, end_char) DO NOTHING
    """
)


def _salary_period(field: ExtractedField | None) -> str | None:
    """The period for the field, in the canonical vocabulary a CHECK allows.

    Thin wrapper over the shared `parse_salary_period` — the word-matching is
    identical whether the string came from an email or a CV, so the rule lives
    once in `evidence.py`. `_value` still handles the "Not mentioned" sentinel
    here because this module's field is an `ExtractedField`, not a bare string.
    """
    return parse_salary_period(_value(field))


def _value(field: ExtractedField | None) -> str | None:
    """`Not mentioned` becomes NULL in the column.

    The raw string still lives on the evidence row, so "the model was asked and
    said the email does not state this" stays distinguishable from "the model
    never answered for this field at all" (plan §15).
    """
    if field is None or field.is_missing:
        return None
    return field.value


# The fields two jobs must share to count as the same vacancy, not two. A model
# that reads "Operations Assistant (Driver) x 2" as two vacancies emits two jobs
# that are byte-identical on these columns — the "x 2" was a headcount on one
# role, not a second role. Requirements and description are deliberately left
# out: two genuinely distinct roles can share a boilerplate requirements block,
# and matching on it would collapse them. These six are the columns a recruiter
# compares to tell rows apart on the list, so they are the columns a dedup that
# runs before insert has to agree on.
_DEDUP_FIELDS = ("job_title", "company", "salary", "location", "working_hours", "duration")


def _dedup_jobs(jobs: list[ExtractedJob]) -> list[ExtractedJob]:
    """Drop jobs identical on the columns that distinguish a vacancy.

    First occurrence wins, so the deterministic `(email, index)` ids below stay
    stable for the jobs that survive — a job at index 2 keeps index 2's id, not
    index 1's, which matters for a retry that must land on the same ids. Only
    exact agreement on every `_DEDUP_FIELDS` column counts; one differing field
    makes two jobs distinct and both are kept.
    """
    seen: set[tuple] = set()
    kept: list[ExtractedJob] = []
    for job in jobs:
        key = tuple(
            (_value(getattr(job, name)) or "").strip().casefold()
            for name in _DEDUP_FIELDS
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(job)
    return kept


def _norm(value: str | None) -> str:
    """Normalise a stored string for comparison: casefolded, stripped.

    Two spellings of the same requirement ("Female" vs "female", " Chinese "
    vs "Chinese") must compare equal; two genuinely different strings must not
    collapse. `None` and the empty string both become the empty string, because
    a field the email never mentioned is stored as NULL and means the same as
    an extracted value of nothing.
    """
    return (value or "").strip().casefold()


def _resolved_salary(
    job: ExtractedJob, source: str
) -> tuple[float | None, float | None, str | None]:
    """The salary range to store: verified structured bounds, else the parse.

    The LLM's `salary_min`/`salary_max` are the richer reading of a compound
    offer ("$4500 basic max + $800 rotating shift allowance" -> 4500–5300) that
    the deterministic `parse_salary` refuses — it fails closed on more than two
    figures. A bound is only trusted when it verified against the source (§15):
    `verify` ran inside `quality_state`, but the raw-text `salary` field is the
    fallback when the model emitted no usable bound (e.g. a pre-schema row, or
    a bound whose evidence did not check out). Every number that lands in the
    columns has therefore either been quoted or arithmetically derived from a
    quote — never authored from nothing.
    """
    lo = _verified_bound(job.salary_min, source)
    hi = _verified_bound(job.salary_max, source)
    if lo is not None or hi is not None:
        return lo, hi, _bound_currency(job)
    if job.salary is not None and not job.salary.is_missing:
        return parse_salary(job.salary.value)
    return None, None, None


def _verified_bound(field: ExtractedField | None, source: str) -> float | None:
    """The bound's value as a number, when the field verified against source.

    `verify` checks the quote is in the email and the value follows from it
    (including the additive-sum rule for these fields); the value then has to
    be a number at all — a model could write "not stated" into the value with
    a real quote behind it, and a non-number must not reach a Numeric column.
    """
    if field is None or field.is_missing:
        return None
    if not verify(field, source, allow_salary_sum=True):
        return None
    try:
        return float(field.value.replace(",", ""))
    except ValueError:
        return None


def _bound_currency(job: ExtractedJob) -> str | None:
    """The currency for a structured salary range.

    The bound fields carry the figures but the raw `salary` field (or either
    bound's own evidence) carries the currency marker, so read it from there —
    the same `_currency` scan the deterministic parser uses.
    """
    for field in (job.salary, job.salary_min, job.salary_max):
        if field is None or field.is_missing:
            continue
        currency = salary_currency(field.evidence or field.value)
        if currency:
            return currency
    return None


def _salary_key(job: ExtractedJob, source: str) -> tuple:
    """The parsed salary of a new job as a comparable tuple.

    Raw salary strings would compare "SGD 6,000" against "6k" as different
    when they are the same figure; the parsed min/max/currency/period is what
    a recruiter compares, so it is what a revision comparison compares.

    Uses the same resolution as `_insert_opportunity` — verified structured
    bounds when present, else the deterministic parse — so a job whose
    compound salary ("$4500 basic + $800 allowance") stores as 4500–5300
    compares against the stored row's 4500–5300, not against a NULL the
    raw parser could not produce.
    """
    salary_min, salary_max, currency = _resolved_salary(job, source)
    return (
        salary_min,
        salary_max,
        currency,
        parse_salary_period(_value(job.salary_period)),
    )


def _row_salary_key(row) -> tuple:
    """The stored salary of an existing opportunity, same tuple shape."""
    return (
        round(float(row.salary_min), 2) if row.salary_min is not None else None,
        round(float(row.salary_max), 2) if row.salary_max is not None else None,
        row.salary_currency,
        row.salary_period,
    )


def _same_vacancy(job: ExtractedJob, row) -> bool:
    """Is the new job the same vacancy as the stored opportunity?

    Identity is company + title + location: the columns a recruiter compares
    to tell one posting from another. The requirements themselves are
    deliberately excluded — whether they changed is the *next* question, and
    comparing them here would make every requirement change a different
    vacancy, which is exactly the bug this whole feature exists to fix.
    """
    return (
        _norm(_value(job.company)) == _norm(row.company_name_raw)
        and _norm(_value(job.job_title)) == _norm(row.job_title_raw)
        and _norm(_value(job.location)) == _norm(row.location_raw)
    )


def _requirements_changed(job: ExtractedJob, codes, job_count: int, row, source: str) -> bool:
    """Did this email state different requirements than the row already holds?

    The fields a client changes when they change a job order: the requirements
    text, the derived sex requirement, the salary, the hours, the duration, the
    employment type, the work arrangement and the skills. Identity (company,
    title, location) is not here — a revision keeps those and changes what the
    job asks for, which is the difference this function exists to see.

    `sex_requirement` is derived from the client's shorthand codes (C/F ->
    female), so an email that drops the code — "open to male, all races" —
    reads as a change, which is correct: the requirement was lifted.

    `source` reaches `_salary_key`, which must verify the structured salary
    bounds against the email before they can be compared — the same resolution
    `_insert_opportunity` uses, so the comparison never pits a verified bound
    against an unverified one.
    """
    new_sex, _ = _sex_requirement_for(job, codes, job_count)
    return not (
        _norm(_value(job.requirements)) == _norm(row.requirements)
        and new_sex == row.sex_requirement
        and _salary_key(job, source) == _row_salary_key(row)
        and _norm(_value(job.working_hours)) == _norm(row.working_hours_raw)
        and _norm(_value(job.duration)) == _norm(row.duration_raw)
        and _norm(_value(job.employment_type)) == _norm(row.employment_type)
        and _norm(_value(job.work_arrangement)) == _norm(row.work_arrangement)
        and sorted(_skills(job)) == sorted(row.skills or [])
    )


async def persist(
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID,
    response: ExtractionResponse,
    result: LLMResult,
    source: str,
    *,
    original_sender_email: str | None = None,
    original_sender_name: str | None = None,
    replay: bool = False,
) -> list[uuid.UUID]:
    """Record one model run and every vacancy it found. Returns the new ids.

    One transaction for the whole run. A partial write — the extraction row
    without its evidence, or two of three vacancies — would look like a
    complete answer to everything downstream, and there is nothing in the data
    that could later tell it apart from one.

    `replay` separates the two ways a job reaches here. The ordinary path
    (`replay=False`) is a fresh extraction or a crash-retry: the deterministic
    opportunity ids collide with the rows the first run wrote, and
    `ON CONFLICT DO NOTHING` leaves them exactly as they were — which is what
    keeps a retry from clobbering a recruiter's claim. A deliberate replay
    (`replay=True`, enqueued by `replay_stale_extractions`) is a re-read of the
    same email under a newer prompt, and its whole point is a *better* answer:
    the extraction-derived columns are refreshed (see `_REPLAYABLE`), while the
    columns a person or the pipeline decided — `assigned_user_id`, `client_id`,
    human corrections in `opportunity_field_overrides`, a `reviewed` sign-off, a
    lawful `sex_requirement` — are left untouched.
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
        sender_row = (await session.execute(_SENDER, {"id": email_message_id})).one_or_none()
        sender_email = sender_row.sender_email if sender_row else None
        sender_name = sender_row.sender_name if sender_row else None
        mailbox_owner_id = sender_row.mailbox_owner_id if sender_row else None
        first_company = next(
            (_value(job.company) for job in response.jobs if _value(job.company)),
            None,
        )
        matched = await match_client(
            session,
            tenant_id,
            email_message_id,
            sender_email,
            first_company,
            mailbox_owner_id=mailbox_owner_id,
            sender_name=sender_name,
            original_sender_email=original_sender_email,
            original_sender_name=original_sender_name,
        )

        # An email describing three vacancies becomes three rows. They share
        # one extraction, because they came from one model call — that is what
        # makes "what did this run cost, and what did it produce" answerable.
        #
        # First, collapse jobs the model split that are one vacancy: a posting
        # that says "Operations Assistant (Driver) x 2" is one role with a
        # headcount of two, and a model that reads the "x 2" as two vacancies
        # emits two jobs identical on every distinguishing column. `_dedup_jobs`
        # drops those before the ids are minted, so one role is one row.
        jobs = _dedup_jobs(response.jobs)
        for index, job in enumerate(jobs):
            opportunity_id = _opportunity_id(email_message_id, index)
            opportunity_ids.append(opportunity_id)
            inserted = await _insert_opportunity(
                session,
                tenant_id,
                email_message_id,
                opportunity_id,
                job,
                source,
                codes,
                len(jobs),
                client_id=matched.client_id if matched else None,
                assigned_user_id=matched.assigned_user_id if matched else None,
                replay=replay,
            )
            # A revision link is one-shot: it must only point at a row this run
            # actually wrote. On a retry the id already exists and `inserted`
            # is False — the first run already linked (or deliberately did
            # not), and re-running the comparison under a different prompt
            # could link against content that is no longer the row's.
            if inserted:
                await _maybe_supersede(
                    session,
                    tenant_id,
                    email_message_id,
                    opportunity_id,
                    job,
                    codes,
                    len(jobs),
                    client_id=matched.client_id if matched else None,
                    source=source,
                )
            await _insert_evidence(session, tenant_id, extraction_id, opportunity_id, job, source)
            await _insert_codes(session, tenant_id, opportunity_id, job, codes, len(jobs))

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
            # A notification is for a vacancy that is new to the recruiter. A
            # replay is not: the row already exists and the recruiter already
            # saw it, so re-notifying would be the duplicate-notification bug
            # the module docstring warns about. Gate on `inserted` — the first
            # insert of a row notifies, and neither a retry (id already written)
            # nor a replay (same) does.
            if inserted:
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
                                    # An assigned job order is one person's work;
                                    # an unassigned one is the queue's, and the
                                    # queue is everybody (`None`). An empty tuple
                                    # would mean nobody at all.
                                    recipient_user_ids=(
                                        (matched.assigned_user_id,)
                                        if matched and matched.assigned_user_id
                                        else None
                                    ),
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
        if isinstance(f, ExtractedField) and f.start_char is not None and f.end_char is not None
    ]
    if not spans:
        return False
    return min(s for s, _ in spans) <= code.start_char and code.end_char <= max(e for _, e in spans)


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


async def _maybe_supersede(
    session,
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID,
    new_opportunity_id: uuid.UUID,
    job: ExtractedJob,
    codes,
    job_count: int,
    client_id: uuid.UUID | None,
    source: str,
) -> None:
    """Link an older open opportunity to this one when the email *revises* it.

    The gap this closes: a client who changes a job order — "initially female,
    Chinese only, now open to male, all races" — sends a *later* email about
    the same vacancy. Today that later email either (a) lands in the same Graph
    conversation and is hidden as a re-forward duplicate, silently keeping the
    stale requirements on the list, or (b) arrives in a new thread and shows up
    as a second open row with nothing tying the two together. Both lose the
    change.

    The rule: an existing **open, current** (`placement_type IS NULL`,
    `superseded_by_opportunity_id IS NULL`) opportunity that is the same
    vacancy — same conversation, or same client + same company/title/location —
    is marked `superseded_by` this new row when the requirements differ. The
    old row stays (append-only history is the audit trail); the read-time
    dedupe then shows the successor and hides the predecessor.

    The comparison is `_requirements_changed`, deliberately *not* "the email
    differs": a re-forward with identical requirements is a duplicate, not a
    revision, and the existing conversation-based dedupe already hides it.

    Only ever points at a row inserted by *this* run (the caller gates on the
    insert's rowcount): a retry re-running under a different prompt must not
    link a new id whose stored content came from an earlier run.
    """
    conv = (
        await session.execute(
            select(EmailMessage.conversation_id).where(EmailMessage.id == email_message_id)
        )
    ).scalar_one_or_none()

    # Same conversation is the primary signal — a re-forward or a reply in the
    # same thread keeps the Graph conversation_id (verified against production,
    # see opportunity_dedupe.py). When the conversation holds no candidate,
    # fall back to the same client + identical role identity, for a genuinely
    # new email about the same vacancy; that requires client_id on the new row
    # or it cannot know which agency's opportunity to look at.
    if conv is None and client_id is None:
        return

    email = aliased(EmailMessage)
    base = (
        select(Opportunity, email.conversation_id)
        .join(email, email.id == Opportunity.email_message_id, isouter=True)
        .where(Opportunity.tenant_id == tenant_id)
        .where(Opportunity.placement_type.is_(None))
        .where(Opportunity.superseded_by_opportunity_id.is_(None))
        .where(Opportunity.id != new_opportunity_id)
        .order_by(Opportunity.received_datetime.asc(), Opportunity.id.asc())
    )
    if conv is not None:
        # Candidates carry their conversation alongside, so the same-conversation
        # filter can run in SQL rather than loading the tenant's whole open
        # pipeline into Python.
        rows = (
            await session.execute(base.where(email.conversation_id == conv))
        ).all()
        candidates = [row[0] for row in rows]
    else:
        candidates = []
    used_fallback = False
    if not candidates and client_id is not None:
        # No match in the thread — try the same client's other open rows.
        rows = (
            await session.execute(base.where(Opportunity.client_id == client_id))
        ).all()
        candidates = [row[0] for row in rows]
        used_fallback = True
    if not candidates:
        return

    # Pick the predecessor(s) this new job is a revision of. Identity is
    # required even for a single open row in the conversation: a follow-up in
    # the same thread can be about a *different* vacancy — the client adds a
    # second role in the same email chain — and without the check that new
    # role would supersede a live job order it has nothing to do with. The
    # user's scenario (same role, requirements changed) matches because
    # `_same_vacancy` compares company/title/location only.
    predecessors = [c for c in candidates if _same_vacancy(job, c)]
    if not predecessors:
        return

    # The cross-conversation fallback (matched on client rather than thread) is
    # only safe when the identity match is unique: a client with two open
    # roles that share company/title/location must not have the second email
    # supersede the first. Within one conversation the threading already
    # disambiguates. The guard keys on the fallback actually being used, not on
    # whether the new email has a conversation: the thread scan can come up
    # empty (all its rows placed or superseded) and the fallback then runs
    # while `conv` is still set.
    if used_fallback and len(predecessors) > 1:
        return

    changed = [c for c in predecessors if _requirements_changed(job, codes, job_count, c, source)]
    if not changed:
        # Identical content. A same-conversation re-forward is already hidden
        # by the read-time dedupe (a later row in the same thread), so there is
        # nothing to write. A *cross-conversation* copy — the client or a buddy
        # sends the same job order again as a fresh email — is not: the dedupe
        # partitions by conversation_id and would let it show as a second open
        # row. Point the new row at the existing one, so the dedupe hides it
        # (a row with `superseded_by` set is hidden) and the loader resolves
        # the copy to the canonical row. `used_fallback` means this is the
        # cross-conversation path and the uniqueness guard above already
        # reduced it to one unambiguous predecessor.
        if used_fallback:
            canonical = predecessors[0]
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == new_opportunity_id)
                .values(
                    superseded_by_opportunity_id=canonical.id,
                    superseded_at=func.now(),
                )
            )
            log.info(
                "opportunity_duplicate_linked",
                tenant_id=str(tenant_id),
                duplicate_opportunity_id=str(new_opportunity_id),
                canonical_opportunity_id=str(canonical.id),
            )
        return

    # Every current open instance of this vacancy is superseded, not just the
    # earliest: an identical re-forward that was never linked stays open and
    # would otherwise become the read-time anchor and keep showing stale rows.
    await session.execute(
        update(Opportunity)
        .where(Opportunity.id.in_([c.id for c in changed]))
        .values(
            superseded_by_opportunity_id=new_opportunity_id,
            superseded_at=func.now(),
        )
    )

    # Carry the human's decisions onto the revision. `_insert_opportunity` set
    # `assigned_user_id` from the client match, which is NULL when the email
    # resolved to no client — but the row being replaced may have been claimed
    # by a recruiter. Without this, a revision of an *assigned* job order
    # silently becomes unassigned: the recruiter who was working it loses it
    # from their view and anyone in the queue can take it. The claim is a
    # person's decision and must outlive the email that restated the vacancy.
    claimed = next((c.assigned_user_id for c in changed if c.assigned_user_id), None)
    if claimed is not None:
        await session.execute(
            update(Opportunity)
            .where(Opportunity.id == new_opportunity_id)
            .where(Opportunity.assigned_user_id.is_(None))
            .values(assigned_user_id=claimed)
        )

    # Same reasoning for a genuine occupational sex requirement a recruiter
    # recorded with a written reason. It is a human judgement about the *job*,
    # not something a client's shorthand email can revoke — a client saying
    # "now open to all races" lifts a preference, not a lawful requirement.
    # Carry it forward when the successor's own extraction did not derive one.
    occupational = next(
        (
            (c.sex_requirement, c.sex_requirement_reason)
            for c in changed
            if c.sex_requirement and c.sex_requirement_reason
        ),
        None,
    )
    if occupational is not None:
        await session.execute(
            update(Opportunity)
            .where(Opportunity.id == new_opportunity_id)
            .where(Opportunity.sex_requirement.is_(None))
            .values(
                sex_requirement=occupational[0],
                sex_requirement_reason=occupational[1],
            )
        )

    log.info(
        "opportunity_superseded",
        tenant_id=str(tenant_id),
        new_opportunity_id=str(new_opportunity_id),
        superseded_ids=[str(c.id) for c in changed],
    )


async def _insert_opportunity(
    session,
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    job: ExtractedJob,
    source: str,
    codes,
    job_count: int,
    client_id: uuid.UUID | None = None,
    assigned_user_id: uuid.UUID | None = None,
    replay: bool = False,
) -> bool:
    """Write one vacancy, or refresh it under a deliberate replay.

    The ordinary path (`replay=False`) is `ON CONFLICT (id) DO NOTHING` — there
    is no update path, and that is what makes the assignment safe under a
    crash-retry. `extract_email` re-runs after a crash, and a retry that
    recomputed `assigned_user_id` would take a job order back off a recruiter
    who had claimed it in the meantime. The claim is a person's decision; the
    match is a guess about a starting point. Only the first insert gets to set
    it.

    A deliberate replay (`replay=True`) is the one deliberate UPDATE this
    module issues. The row already exists — a retry produced the same
    deterministic id and inserted nothing — and the whole point of replaying is
    a better answer under a newer prompt. So the extraction-derived columns are
    refreshed, subject to the same discipline as everything else here:

    - `assigned_user_id` and `client_id` are never touched (a claim / a match,
      both possibly corrected by a person since the first run).
    - a column with a human correction in `opportunity_field_overrides` is
      skipped — replay must never overwrite a recruiter's fix.
    - `review_status = 'reviewed'` is preserved: a person signed off the row,
      and a re-read of the same email does not undo that.
    - `sex_requirement` is preserved when a person set it (`sex_requirement_set_by`
      is not NULL); only the pipeline-derived value is refreshed.

    `sex_requirement` is derived from the client's shorthand codes that apply to
    this vacancy (`C/F`/`O/F` → female), set alongside an audit reason naming the
    codes. Both are NULL when no sex is implied — the ordinary case.
    """
    salary_min, salary_max, currency = _resolved_salary(job, source)

    sex_requirement, sex_requirement_reason = _sex_requirement_for(job, codes, job_count)
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
        "client_id": client_id,
        "assigned_user_id": assigned_user_id,
        "sex_requirement": sex_requirement,
        "sex_requirement_reason": sex_requirement_reason,
    }
    params.update({name: _value(getattr(job, name)) for name in _SIMPLE})
    result = await session.execute(_INSERT_OPPORTUNITY, params)
    # `ON CONFLICT (id) DO NOTHING` rowcount: 1 when the row was newly written,
    # 0 when this is a replay of an id that already exists. Callers gate
    # one-shot side effects on it — in particular the supersede link below must
    # only ever be pointed at a row that actually appeared in this run, or a
    # retry under a different prompt would link against an id whose stored
    # content came from the *previous* run.
    inserted = result.rowcount == 1
    if not inserted and replay:
        # The refresh is keyed by COLUMN name, while `params` above is keyed by
        # model field name for the `_SIMPLE` fields — replay maps them across so
        # `_refresh_opportunity` can compare against `opportunity_field_overrides`
        # (whose `field_name` is the DB column name).
        column_params = dict(params)
        for field_name, column in _SIMPLE.items():
            column_params[column] = params[field_name]
        await _refresh_opportunity(session, opportunity_id, column_params)
    return inserted


async def _refresh_opportunity(session, opportunity_id: uuid.UUID, params: dict) -> None:
    """Refresh a row in place after a deliberate replay.

    Writes only the columns in `_REPLAYABLE`, minus any column a person
    corrected in `opportunity_field_overrides`. A human fix is the one thing
    this module must never clobber, so the overrides are read in the same
    transaction as the write — a correction landing between the two would be
    overwritten, which is exactly the race this ordering closes.

    `review_status` and `sex_requirement` carry their own guards inside the
    UPDATE: `reviewed` is a person's sign-off and `sex_requirement_set_by` marks
    a lawful judgement, and neither is a fresh read of the same email entitled
    to erase.
    """
    overridden = {
        row[0]
        for row in (
            await session.execute(_SELECT_OVERRIDES, {"id": opportunity_id})
        ).all()
    }
    columns = sorted(_REPLAYABLE - overridden)
    if not columns:
        return
    setters = []
    values = {"id": opportunity_id}
    for column in columns:
        if column == "review_status":
            setters.append(
                "review_status = CASE WHEN review_status = 'reviewed'"
                " THEN review_status ELSE :review_status END"
            )
            values["review_status"] = params["review_status"]
        elif column == "sex_requirement":
            setters.append(
                "sex_requirement = CASE WHEN sex_requirement_set_by IS NULL"
                " THEN :sex_requirement ELSE sex_requirement END"
            )
            values["sex_requirement"] = params["sex_requirement"]
        elif column == "sex_requirement_reason":
            setters.append(
                "sex_requirement_reason = CASE WHEN sex_requirement_set_by IS NULL"
                " THEN :sex_requirement_reason ELSE sex_requirement_reason END"
            )
            values["sex_requirement_reason"] = params["sex_requirement_reason"]
        else:
            setters.append(f"{column} = :{column}")
            values[column] = params[column]
    await session.execute(
        text(f"UPDATE opportunities SET {', '.join(setters)} WHERE id = :id"),
        values,
    )


def _skills(job: ExtractedJob) -> list[str]:
    """The model returns one string; the column is an array.

    Split rather than stored whole so a skill is queryable on its own. Empty
    rather than NULL when nothing was extracted: an absent skill list and an
    empty one mean the same thing to every query that would read it.
    """
    raw = _value(job.skills) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _codes_for_vacancy(
    job: ExtractedJob, codes: list[DetectedCode], job_count: int
) -> list[DetectedCode]:
    """The codes that apply to this one vacancy.

    A single-vacancy email hands every code to that vacancy; beyond one, the
    span check in `_covers` decides — the same rule `_insert_codes` uses, so the
    requirement and the decoded shorthand can never disagree about which codes a
    row was built from.
    """
    if job_count <= 1:
        return list(codes)
    return [code for code in codes if _covers(job, code)]


def _sex_requirement_for(job: ExtractedJob, codes, job_count: int):
    """The sex requirement and a reason, derived from this vacancy's shorthand.

    `C/F` / `O/F` imply female; `C/M` / `O/M` imply male. When every applicable
    sex-bearing code agrees, that sex becomes the row's `sex_requirement`. The
    reason is required alongside it (the database CHECK
    `ck_opportunities_sex_requirement_has_reason` refuses one without the other),
    so it records plainly that the requirement came from the client's email
    shorthand, naming the codes — an audit trail, not a justification a person
    wrote. Returns `(None, None)` when no sex is implied, which is the ordinary
    case for most vacancies.
    """
    applicable = _codes_for_vacancy(job, codes, job_count)
    sex = implied_sex(applicable)
    if sex is None:
        return None, None
    # allow-hardcode: an audit sentence naming the codes found, not configuration.
    found = ", ".join(sorted({c.code for c in applicable}))
    reason = f"Set from the client's shorthand in the source email: {found}."
    return sex, reason


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
        #
        # The salary bounds are verified with the additive-sum rule: a bound
        # may be a figure the email never wrote in full ("$4500 basic + $800
        # allowance" -> 5300), and that is the only derived number the model
        # is allowed to author. `quality_state` uses the same flag, so the
        # evidence row and the row's verdict can never disagree.
        valid = verify(
            field,
            source,
            allow_salary_sum=name in ("salary_min", "salary_max"),
        )
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
