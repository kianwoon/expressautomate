"""Write an extraction and its opportunities in one transaction (plan §14).

Append-only with respect to history: every run inserts a new `extractions` row
and new opportunities. Nothing is updated in place, so an email's extraction
history is the ordered set of its rows — and a prompt upgrade replayed across a
year of mail adds to that history rather than rewriting it.

Human corrections live in `opportunity_field_overrides` and are never read or
written here. That separation is what makes replay safe: this module physically
cannot clobber a recruiter's fix, because it never issues an UPDATE against
anything a human has touched.
"""

import json
import uuid

from sqlalchemy import ARRAY, Text, bindparam, text

from app.core.config import settings
from app.db.rls import tenant_session
from app.services.ingest.evidence import parse_salary, quality_state, verify
from app.services.ingest.schema import ExtractedField, ExtractedJob, ExtractionResponse
from app.services.llm.client import LLMResult

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
    """
).bindparams(bindparam("skills", type_=ARRAY(Text)))

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

        # An email describing three vacancies becomes three rows. They share
        # one extraction, because they came from one model call — that is what
        # makes "what did this run cost, and what did it produce" answerable.
        for job in response.jobs:
            opportunity_id = uuid.uuid4()
            opportunity_ids.append(opportunity_id)
            await _insert_opportunity(
                session, tenant_id, email_message_id, opportunity_id, job, source
            )
            await _insert_evidence(
                session, tenant_id, extraction_id, opportunity_id, job, source
            )

    return opportunity_ids


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
        "salary_period": _value(job.salary_period),
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
