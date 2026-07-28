"""Turning a read CV into rows a person can accept or refuse (plan §14, §15).

Everything here proposes. A role or skill that arrives from a CV lands
`source="cv_upload"`, `status="unconfirmed"`, and waits for a human — the
same lifecycle a role typed by a recruiter skips because a recruiter is
already the human. Nothing this module writes ever overwrites a row somebody
else wrote.

The provenance writes are lifted from `app.services.ingest.persist` rather
than invented: one `extractions` row per model run, one `extraction_evidence`
row per field the model answered for, valid or not. Keeping the invalid ones
is the point there and here — a span that is not on the page is the only
record that the model claimed something the document does not say, and that
record is what makes a prompt regression visible later.

**The matching rule.** A parsed role is the same role as an existing one when
they share a normalised employer and their spans overlap. Both spans are
pinned to month starts by `candidate_tenure.span_months`, so "Mar 2019" off a
CV and `2019-03-14` typed by a recruiter describe the same month; overlap is
half-open, so a role ending March 2020 and one starting March 2020 are two
roles, which is what somebody who moved jobs actually did. A parsed role with
no dates at all can only be matched on the employer, and only when exactly one
existing role carries it — with nothing to disambiguate on, choosing among two
would be a guess presented as a fact, so it is inserted unconfirmed instead
and a person decides.

An existing role that itself carries no dates is deliberately not compared
against a dated parsed role: there is no span to compare, and matching on the
employer alone in that direction would silently absorb a second, real
position at the same company.

A rejected role is still a candidate for this match — the reject endpoint
promises a re-parse will not resurrect what a recruiter threw out, and that
only holds if a rejected row can still be recognised. A match against one
does not reopen it: nothing is inserted, and the rejected row is left
exactly as rejected.
"""

import json
import re
import uuid
from datetime import date

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.models.candidate import CandidateDocument, CandidateRole
from app.services.candidate_naming import normalize_skill
from app.services.candidate_tenure import span_months
from app.services.client_naming import normalize_company_name
from app.services.cv.schema import ROLE_FIELDS, CVResponse, ExtractedDate, ExtractedRole
from app.services.ingest.evidence import verify
from app.services.ingest.schema import ExtractedField
from app.services.llm.client import LLMResult

log = get_logger(__name__)

SOURCE_CV = "cv_upload"

# allow-hardcode: SQL statements, not a phrase list.
_INSERT_EXTRACTION = text(
    """
    INSERT INTO extractions
        (id, tenant_id, candidate_document_id, model_name, prompt_version,
         prompt_tokens, completion_tokens, latency_ms, raw_response)
    VALUES (:id, :tenant_id, :candidate_document_id, :model_name, :prompt_version,
            :prompt_tokens, :completion_tokens, :latency_ms,
            CAST(:raw_response AS jsonb))
    """
)

_INSERT_EVIDENCE = text(
    """
    INSERT INTO extraction_evidence
        (id, tenant_id, extraction_id, candidate_role_id, field_name,
         extracted_value, evidence_text, start_char, end_char,
         model_confidence, evidence_valid)
    VALUES (:id, :tenant_id, :extraction_id, :candidate_role_id, :field_name,
            :extracted_value, :evidence_text, :start_char, :end_char,
            :model_confidence, :evidence_valid)
    """
)

_EXISTING_ROLES = text(
    """
    SELECT id, employer_normalized, started_on, started_precision,
           ended_on, ended_precision, status
    FROM candidate_roles
    WHERE candidate_id = :candidate_id
    """
)

_INSERT_ROLE = text(
    """
    INSERT INTO candidate_roles
        (id, tenant_id, candidate_id, employer, employer_normalized,
         title, title_normalized, started_on, started_precision,
         ended_on, ended_precision, description, source, status, extraction_id)
    VALUES (:id, :tenant_id, :candidate_id, :employer, :employer_normalized,
            :title, :title_normalized, :started_on, :started_precision,
            :ended_on, :ended_precision, :description, :source, :status,
            :extraction_id)
    """
)

# The unique index is what makes a replay idempotent, so the conflict clause
# names it rather than a column list: a skill already held stays exactly as it
# is, including a `confirmed` status a person gave it, and the parse neither
# duplicates it nor quietly demotes it back to a proposal.
_INSERT_SKILL = text(
    """
    INSERT INTO candidate_skills
        (id, tenant_id, candidate_id, skill, skill_normalized, source, status)
    VALUES (:id, :tenant_id, :candidate_id, :skill, :skill_normalized,
            :source, :status)
    ON CONFLICT ON CONSTRAINT uq_candidate_skills_once_per_candidate DO NOTHING
    """
)

# allow-hardcode: the vocabulary of written dates, not configuration. Kept
# beside `cv.schema`'s shape regexes rather than shared with them because they
# answer different questions — those decide what precision a quotation could
# honestly carry, these read an actual value out of it.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_WORD = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_ISO = re.compile(r"\b(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?\b")
_DMY = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")
_DAY_MONTH_YEAR = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_WORD}\s*,?\s*(\d{{4}})\b", re.IGNORECASE
)
_MONTH_DAY_YEAR = re.compile(
    rf"\b{_MONTH_WORD}\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(\d{{4}})\b", re.IGNORECASE
)
_MONTH_YEAR = re.compile(rf"\b{_MONTH_WORD}\s*,?\s*(\d{{4}})\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(\d{4})\b")


def _read_parts(value: str) -> tuple[int | None, int | None, int | None]:
    """Year, month and day as far as the written value actually carries them.

    A missing part is None rather than a default. Filling one in here is
    precisely the fabrication §15 forbids: the caller decides whether the
    parts it got are enough for the precision the model claimed, and refuses
    the date otherwise.
    """
    match = _ISO.search(value)
    if match:
        year, month, day = match.groups()
        return int(year), int(month), int(day) if day else None

    match = _DMY.search(value)
    if match:
        day, month, year = match.groups()
        day, month = int(day), int(month)
        # Day-first when it's unambiguous: this deployment serves Singapore,
        # so 25/4/2019 is the 25th of April, and if the first field exceeds
        # 12 there is only one legal reading anyway. But when both fields are
        # <=12 the CV genuinely does not say which is the day and which is
        # the month — 3/4/2019 could honestly be either. Guessing day-first
        # there would store a fabricated fact (§15), so only the year is
        # trustworthy; do not "fix" this back into a guess.
        if day <= 12 and month <= 12:
            return int(year), None, None
        return int(year), month, day

    match = _DAY_MONTH_YEAR.search(value)
    if match:
        day, month, year = match.groups()
        return int(year), _MONTHS[month[:3].lower()], int(day)

    match = _MONTH_DAY_YEAR.search(value)
    if match:
        month, day, year = match.groups()
        return int(year), _MONTHS[month[:3].lower()], int(day)

    match = _MONTH_YEAR.search(value)
    if match:
        month, year = match.groups()
        return int(year), _MONTHS[month[:3].lower()], None

    match = _YEAR.search(value)
    if match:
        return int(match.group(1)), None, None

    return None, None, None


def stored_date(field: ExtractedDate | None) -> tuple[date | None, str | None]:
    """The date to store, and the precision it is stored at.

    The precision persisted is the one the model reported and the schema
    already refused to let run ahead of the quotation — it is never recomputed
    here and never made finer. A date column needs a full date, so a month
    lands on the first of that month and a year on the first of January, but
    the precision column travels with it and the UI renders only what the page
    said. Recording `day` for "Mar 2019" would put a first-of-the-month the
    candidate never wrote in front of a client as fact.

    Returns `(None, None)` when the written value cannot support the claimed
    precision. Dropping the date is the honest outcome: the role is still
    worth keeping, and a half-read date is not.
    """
    if field is None or field.is_missing or field.precision is None:
        return None, None

    year, month, day = _read_parts(field.value or "")
    if year is None:
        return None, None
    precision = field.precision
    if precision == "day" and day is None:
        # Either the value legitimately carries no day (a month-shaped
        # quotation the model over-claimed), or it is the ambiguous
        # numeric case `_read_parts` refused to guess at — either way,
        # the year is still real. Report it at the precision it can
        # actually support rather than dropping the role entirely.
        precision = "month" if month is not None else "year"
    if precision == "month" and month is None:
        precision = "year"

    try:
        return date(year, month or 1, day or 1), precision
    except ValueError:
        # A month of 13 or a 31st of February. The model wrote something that
        # is not a date; nothing here is going to repair it.
        return None, None


def _for_matching(span: tuple[date, date]) -> tuple[date, date]:
    """A span, widened so a same-month role can still be found.

    `span_months` correctly pins both ends to month starts and clamps a
    same-month role to zero length — that arithmetic is right and other code
    depends on it staying that way. But a half-open overlap test can never be
    true for a zero-length span on either side, so a role that started and
    ended in the same calendar month could never match anything, typed or
    parsed. For matching only, such a role is widened to occupy its one
    month; this function is never used for tenure arithmetic.
    """
    start, end = span
    if end == start:
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


def _overlaps(a: tuple[date, date], b: tuple[date, date]) -> bool:
    """Half-open overlap. Adjacency is not overlap — it is two jobs."""
    a, b = _for_matching(a), _for_matching(b)
    return a[0] < b[1] and b[0] < a[1]


def match_existing_role(
    employer_normalized: str,
    started_on: date | None,
    started_precision: str | None,
    ended_on: date | None,
    ended_precision: str | None,
    existing: list,
    today: date,
) -> uuid.UUID | None:
    """The id of the role this parsed one already is, if any.

    Pure, so the rule can be tested without a database in the way — the same
    reason `candidate_tenure` is a separate module.
    """
    same_employer = [
        row for row in existing if row.employer_normalized == employer_normalized
    ]
    if not same_employer:
        return None

    if started_on is None:
        # Nothing to compare but the company. One row is an unambiguous
        # answer; two or more is a coin toss, and a coin toss recorded as a
        # match is a fabricated identity.
        return same_employer[0].id if len(same_employer) == 1 else None

    span = span_months(started_on, started_precision, ended_on, ended_precision, today)
    for row in same_employer:
        if row.started_on is None:
            continue
        other = span_months(
            row.started_on, row.started_precision, row.ended_on, row.ended_precision, today
        )
        if _overlaps(span, other):
            return row.id
    return None


def _stated(field: ExtractedField | None) -> str | None:
    return None if field is None or field.is_missing else field.value


def _dropped_by_verification(response: CVResponse, result: LLMResult) -> tuple[int, str | None]:
    """How much of the model's own answer never made it past verification.

    `extract_cv` discards a role or skill whose quotation is not on the page
    and says nothing about it, which is right for the data and wrong for the
    person reading the result: a five-job CV that yields three roles otherwise
    looks like a three-job CV. The raw answer is still on `result.data`, so
    the count is a subtraction rather than a change to the extractor.
    """
    raw = result.data if isinstance(result.data, dict) else {}
    raw_roles = raw.get("roles")
    raw_skills = raw.get("skills")
    roles = len(raw_roles) - len(response.roles) if isinstance(raw_roles, list) else 0
    skills = len(raw_skills) - len(response.skills) if isinstance(raw_skills, list) else 0
    roles, skills = max(roles, 0), max(skills, 0)
    if not roles and not skills:
        return 0, None

    parts = []
    if roles:
        parts.append(f"{roles} role(s)")
    if skills:
        parts.append(f"{skills} skill(s)")
    return roles + skills, (
        f"{' and '.join(parts)} were left out because the text quoted for them "
        "could not be found in this CV."
    )


async def _insert_evidence(
    session,
    tenant_id: uuid.UUID,
    extraction_id: uuid.UUID,
    role_id: uuid.UUID | None,
    fields: dict[str, ExtractedField],
    source: str,
) -> None:
    for name, field in fields.items():
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
                "candidate_role_id": role_id,
                "field_name": name,
                "extracted_value": field.value,
                "evidence_text": field.evidence,
                "start_char": field.start_char,
                "end_char": field.end_char,
                "model_confidence": field.confidence,
                "evidence_valid": valid,
            },
        )


def _role_fields(role: ExtractedRole) -> dict[str, ExtractedField]:
    return {
        name: field
        for name in ROLE_FIELDS
        if isinstance(field := getattr(role, name), ExtractedField) and not field.is_missing
    }


async def persist_cv(
    session,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document: CandidateDocument,
    response: CVResponse,
    result: LLMResult,
    text: str,
    today: date | None = None,
) -> None:
    """Record one CV parse: the run, what it found, and what it left out.

    Takes the caller's session rather than opening its own, so the document's
    move out of `parsing` and everything this writes land in one transaction.
    A partial commit here would be indistinguishable from a complete answer to
    everything downstream — a document reading `parsed` with half its roles is
    a career the recruiter has no reason to doubt.

    The document's outcome columns are set here and not by the job, because
    only this function knows whether anything survived: `empty` is a real
    answer, and one the job cannot tell from a success without redoing the
    same work.
    """
    extraction_id = uuid.uuid4()
    await session.execute(
        _INSERT_EXTRACTION,
        {
            "id": extraction_id,
            "tenant_id": tenant_id,
            "candidate_document_id": document.id,
            "model_name": result.model,
            "prompt_version": settings.PROMPT_VERSION,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "latency_ms": result.latency_ms,
            "raw_response": json.dumps(result.raw),
        },
    )

    existing = list(
        (await session.execute(_EXISTING_ROLES, {"candidate_id": candidate_id})).all()
    )
    # Injected as a keyword with a default, matching `candidate_tenure.derive`,
    # so the open-ended-role path can be tested without waiting on the clock.
    today = today or date.today()

    # `existing` now carries rejected rows too, purely so a parsed role that
    # matches one can be recognised — the lookup below is what keeps the
    # rejection standing rather than the match itself.
    rejected_ids = {row.id for row in existing if row.status == CandidateRole.REJECTED}

    dropped, reason = _dropped_by_verification(response, result)
    unusable = 0
    inserted_roles = 0
    matched_roles = 0
    rejected_matches = 0

    for role in response.roles:
        employer = _stated(role.company)
        title = _stated(role.title)
        fields = _role_fields(role)
        if not employer or not title:
            # `candidate_roles` requires both, and inventing either — a title
            # from a company, a company from a title — is the one thing §15
            # forbids outright. The evidence is still written, pointing at no
            # role, so the fragment the model did read is not lost.
            unusable += 1
            await _insert_evidence(session, tenant_id, extraction_id, None, fields, text)
            continue

        employer_normalized = normalize_company_name(employer)
        started_on, started_precision = stored_date(role.start_date)
        ended_on, ended_precision = stored_date(role.end_date)
        # The check constraint refuses an end before a start, and a CV that
        # gives them in the wrong order is a CV, not a bug to crash on.
        if started_on and ended_on and ended_on < started_on:
            started_on = started_precision = ended_on = ended_precision = None

        match = match_existing_role(
            employer_normalized,
            started_on,
            started_precision,
            ended_on,
            ended_precision,
            existing,
            today,
        )
        if match is not None and match in rejected_ids:
            # A recruiter already looked at this role and threw it out, and
            # the reject endpoint's own docstring promises a re-parse will not
            # bring it back. The row itself is left exactly as rejected, but
            # the evidence is *not* attached to it: a rejected role showing no
            # evidence in the UI would otherwise quietly grow a pile of
            # corroboration nobody who rejected it will ever see. Writing it
            # with no role instead keeps the match visible in the extraction
            # record — the same place an unusable role's evidence goes — and
            # counted below, without pretending the rejection reopened it.
            await _insert_evidence(session, tenant_id, extraction_id, None, fields, text)
            rejected_matches += 1
            continue

        if match is not None:
            # The row already exists and may carry a human's corrections, so
            # nothing about it is touched. The evidence still points at it:
            # what the CV said about a role a recruiter typed is exactly the
            # provenance somebody checking that row will want.
            await _insert_evidence(session, tenant_id, extraction_id, match, fields, text)
            matched_roles += 1
            continue

        role_id = uuid.uuid4()
        await session.execute(
            _INSERT_ROLE,
            {
                "id": role_id,
                "tenant_id": tenant_id,
                "candidate_id": candidate_id,
                "employer": employer,
                "employer_normalized": employer_normalized,
                "title": title,
                # The plainest normalisation there is, matching what the roles
                # API does when a person types one.
                "title_normalized": title.strip().lower(),
                "started_on": started_on,
                "started_precision": started_precision,
                "ended_on": ended_on,
                "ended_precision": ended_precision,
                "description": _stated(role.summary),
                "source": SOURCE_CV,
                "status": CandidateRole.UNCONFIRMED,
                "extraction_id": extraction_id,
            },
        )
        await _insert_evidence(session, tenant_id, extraction_id, role_id, fields, text)
        inserted_roles += 1
        # So a CV listing the same employer twice cannot match its own first
        # row on the second pass and silently collapse two positions into one.
        existing.append(
            _NewRole(role_id, employer_normalized, started_on, started_precision,
                     ended_on, ended_precision, CandidateRole.UNCONFIRMED)
        )

    inserted_skills = 0
    blank_skills = 0
    for skill in response.skills:
        value = (skill.value or "").strip()
        if not value:
            # Blank after stripping is still a skill the model claimed and
            # verification let through; leaving it out of `dropped_count`
            # would under-report exactly like the roles case below.
            blank_skills += 1
            continue
        await session.execute(
            _INSERT_SKILL,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "candidate_id": candidate_id,
                "skill": value,
                "skill_normalized": normalize_skill(value),
                "source": SOURCE_CV,
                "status": CandidateRole.UNCONFIRMED,
            },
        )
        await _insert_evidence(
            session, tenant_id, extraction_id, None, {"skill": skill}, text
        )
        inserted_skills += 1

    if unusable:
        note = (
            f"{unusable} role(s) were left out because the CV named no employer "
            "or no job title for them."
        )
        reason = f"{reason} {note}" if reason else note
        dropped += unusable

    if rejected_matches:
        note = (
            f"{rejected_matches} role(s) were left out because they match one "
            "you already rejected."
        )
        reason = f"{reason} {note}" if reason else note
        dropped += rejected_matches

    if blank_skills:
        note = f"{blank_skills} skill(s) were left out because the CV named none."
        reason = f"{reason} {note}" if reason else note
        dropped += blank_skills

    document.dropped_count = dropped
    document.dropped_reason = reason
    # Nothing survived: no role inserted, no role recognised, no skill. That
    # is a document that is probably not a CV — a covering letter, a scan with
    # no text layer — and it must not read the same as a full career. Counted
    # from what was actually stored or matched rather than from
    # `response.roles`, which still holds roles too incomplete to be a row.
    document.parse_state = (
        CandidateDocument.PARSED
        if inserted_roles or matched_roles or inserted_skills or rejected_matches
        else CandidateDocument.EMPTY
    )

    log.info(
        "cv_persisted",
        candidate_document_id=str(document.id),
        roles=inserted_roles,
        skills=inserted_skills,
        dropped=dropped,
        model=result.model,
    )


class _NewRole:
    """A row this run just inserted, shaped like one read back from the table.

    The six attributes `match_existing_role` reads, plus `status` so this run's
    own inserts can sit in the same `existing` list as rows read from the
    table without a second, differently-shaped rejected-row check. A real
    read-back would cost a query per role for information we already hold.
    """

    __slots__ = (
        "id", "employer_normalized", "started_on", "started_precision",
        "ended_on", "ended_precision", "status",
    )

    def __init__(self, id, employer_normalized, started_on, started_precision,
                 ended_on, ended_precision, status):
        self.id = id
        self.employer_normalized = employer_normalized
        self.started_on = started_on
        self.started_precision = started_precision
        self.ended_on = ended_on
        self.ended_precision = ended_precision
        self.status = status
