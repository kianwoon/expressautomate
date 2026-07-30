"""The job orders, as a recruiter would read them (plan §16, §17).

This is the spreadsheet the product replaces, rendered from extracted rows —
plus the column the spreadsheet never had: when the email arrived. A vacancy
mailed six weeks ago and one mailed this morning look identical in a sheet
that never recorded the date, and the recruiter working the list has no way
to tell which is still open.

Read-only and raw-first. Every `_raw` value is returned beside its normalised
form because the raw string is the one a recruiter recognises from the email;
showing only "SGD 6000/month" for a mail that said "6k neg." is a paraphrase
presented as a quotation.

Nothing here substitutes a value for a missing one. A field the email did not
mention comes back as null and stays null all the way to the screen (§15) —
an empty string or a zero would be indistinguishable from an extracted value
of nothing, which is exactly the fabrication the pipeline is built to avoid.

Two later decisions shape the payload. The chip counts cover everything the
caller may see and are computed apart from the page, because a count that shrank as you paged would
be answering a different question than the one it appears to answer. And the
trust signal is `verified_fields / total_fields` — how many extracted values
were found verbatim in the source — not a model confidence score.
`model_confidence` exists for calibration and is never rendered as a
probability (see `models/extraction.py`); it does not appear in any response
here, and a test asserts that it never starts to.
"""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, model_validator
from sqlalchemy import String, case, cast, func, or_, select, update
from sqlalchemy.orm import aliased

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.db.rls import tenant_session
from app.models import Candidate, EmailMessage, Opportunity, OpportunityCode
from app.models.extraction import ExtractionEvidence
from app.services.sourcing import eligibility
from app.services.visibility import (
    load_editable_opportunity,
    load_visible_opportunity,
    visible_opportunities,
)

router = APIRouter(tags=["opportunities"])

# The stored column and the word the UI shows are deliberately not the same.
# `persist.py` writes `ready` for a clean extraction and owns that value; this
# module must not redefine it. But "ready" reads to a recruiter as *finished*,
# which is the opposite of what the chip means — an untouched row. So the API
# renames it to `new` on the way out and back on the way in, and the third
# state, `reviewed`, is the only one a human writes.
_READY = "ready"
_NEEDS_REVIEW = "needs_review"
_REVIEWED = "reviewed"

# Filter name -> stored value. Anything absent from this map is not a filter a
# caller may ask for, which is why the lookup is a dict and not string concat.
_FILTER_TO_STORED = {"new": _READY, _NEEDS_REVIEW: _NEEDS_REVIEW, _REVIEWED: _REVIEWED}
_STORED_TO_FILTER = {stored: name for name, stored in _FILTER_TO_STORED.items()}

StatusFilter = Literal["new", "needs_review", "reviewed"]

SortKey = Literal[
    "received", "company", "position", "salary", "hours", "duration", "location", "quality"
]

# How many of each period make a month, carried over verbatim from
# `frontend/app/dashboard/job-orders-sort.ts` (`PER_MONTH`, ~line 55). A
# different factor here would silently reorder a recruiter's list relative to
# what the same column showed before the sort moved server-side. A period
# absent from this map — including a NULL `salary_period` — is not assumed
# monthly; it falls through to NULL in the CASE below and sinks with the rest
# of the missing values, same as the client did.
_SALARY_PER_MONTH = {
    "hour": 1 / (40 * 4.35),
    "day": 1 / 21.75,
    "week": 1 / 4.35,
    "month": 1,
    "year": 12,
}

# Worst first when ascending, carried over verbatim from `QUALITY_RANK` in
# job-orders-sort.ts (~line 84): the rows that need a human are the reason
# anyone sorts this column at all.
_QUALITY_RANK = {"needs_review": 0, "likely": 1, "verified": 2}


class ReviewRequest(BaseModel):
    """Explicitly two-way: the same endpoint un-reviews.

    A one-way "mark reviewed" leaves a mis-click permanent, and the only escape
    a recruiter would have is to stop trusting the chip counts.
    """

    reviewed: bool


class PlacementTypeRequest(BaseModel):
    """`placement_type` is set by a person, never inferred — see the column
    comment in `app/models/opportunity.py`. `None` clears it back to "not
    stated"."""

    placement_type: Literal[
        "local_hire", "mdw_work_permit", "other_work_permit", "s_pass", "employment_pass"
    ] | None = None


class OccupationalRequirementRequest(BaseModel):
    """A genuine occupational sex requirement plus the recruiter's own words
    for why. Mirrors the database's pairing CHECK
    (`ck_opportunities_sex_requirement_has_reason`) so a bad request is a 422
    here rather than surfacing as a raw constraint violation."""

    sex_requirement: Literal["female", "male"] | None = None
    sex_requirement_reason: str | None = None

    @model_validator(mode="after")
    def _reason_required_with_requirement(self) -> "OccupationalRequirementRequest":
        has_requirement = self.sex_requirement is not None
        has_reason = bool((self.sex_requirement_reason or "").strip())
        if has_requirement != has_reason:
            raise ValueError(
                "A sex requirement needs a reason in the recruiter's own words, "
                "and a reason needs a requirement to explain."
            )
        return self


@router.get("/opportunities")
async def list_opportunities(
    request: Request,
    # Resolved in the body rather than declared as `= settings.…`: a default in
    # the signature is bound once at import, so the setting would freeze at the
    # value it had when the module loaded and no later change could move it.
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    status: StatusFilter | None = None,
    q: str | None = None,
    sort: SortKey = "received",
    descending: bool = True,
) -> dict:
    """The signed-in user's agency's vacancies, newest first.

    Deliberately *not* gated on the mailbox scope, unlike the endpoints in
    `mailbox.py`: a revoked grant stops new mail arriving, it does not make
    the job orders already extracted anyone else's business. Locking the list
    behind a live grant would blank the page for the one user who most needs
    to see what was collected before it broke.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    # RLS keeps this inside the agency; this clause decides which recruiter
    # inside that agency sees which job order. Both the counts and the page
    # take it, and they must take the same one — a count over rows the page
    # will not show tells a recruiter there are twelve and then shows four.
    visible = visible_opportunities(user_uuid, role)

    ceiling = settings.OPPORTUNITIES_PAGE_LIMIT
    # Clamped, not rejected. A caller asking for more than the page holds is
    # asking for the page; 400ing them would break the list over a number the
    # operator is free to lower at any time.
    page_limit = ceiling if limit is None else min(limit, ceiling)

    # Every read goes through `tenant_session`, which sets `app.tenant_id` for
    # the transaction. Without it RLS returns zero rows rather than everyone's
    # — the failure is visible, but it is still a failure, and a plain
    # `SessionLocal()` here would be one edit away from a cross-agency leak.
    async with tenant_session(tenant_uuid) as session:
        # The chips are counted over everything visible to this caller, in
        # their own query,
        # before any filter or window is applied. A count that moved with the
        # page would tell the recruiter there are 12 vacancies needing review
        # on page one and 3 on page two, which is not a smaller truth — it is
        # a different question than the one the chip appears to answer.
        counts = {name: 0 for name in _FILTER_TO_STORED}
        counts["all"] = 0
        for stored, n in await session.execute(
            select(Opportunity.review_status, func.count())
            .where(visible)
            .group_by(Opportunity.review_status)
        ):
            counts["all"] += n
            # A stored value this API has no name for (a future state written
            # by the pipeline) still counts towards `all` but is not invented
            # as a chip. Silently dropping it from `all` would make the totals
            # disagree with the list, which is the harder bug to see.
            if stored in _STORED_TO_FILTER:
                counts[_STORED_TO_FILTER[stored]] += n

        # `email_messages` carries the two message ids, so the list joins it
        # rather than denormalising them onto the opportunity: they exist to
        # let a recruiter open the original mail, and a copy that drifts from
        # the source is worse than a join.
        email = aliased(EmailMessage)
        base = (
            select(Opportunity, email.internet_message_id, email.graph_message_id)
            .join(email, email.id == Opportunity.email_message_id)
            .where(visible)
        )
        if status is not None:
            base = base.where(Opportunity.review_status == _FILTER_TO_STORED[status])

        if q:
            # Escape LIKE metacharacters (%, _) with backslash so they are
            # treated as literals, not as SQL wildcards. Same approach as
            # `candidates.py` — this is not a SQL-injection concern (the value
            # is parameterized), but without escaping a recruiter searching
            # for a literal "%" or "_" gets wrong results.
            normalized = q.strip().lower()
            escaped = (
                normalized.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            like = f"%{escaped}%"
            base = base.where(
                or_(
                    func.lower(Opportunity.company_name_raw).like(like, escape="\\"),
                    func.lower(Opportunity.job_title_raw).like(like, escape="\\"),
                    func.lower(Opportunity.salary_raw).like(like, escape="\\"),
                    func.lower(Opportunity.working_hours_raw).like(like, escape="\\"),
                    func.lower(Opportunity.duration_raw).like(like, escape="\\"),
                    func.lower(Opportunity.location_raw).like(like, escape="\\"),
                    func.lower(Opportunity.requirements).like(like, escape="\\"),
                    func.lower(Opportunity.job_description).like(like, escape="\\"),
                    # `salaryRange` on the client used to be searchable text —
                    # a rendered string like "SGD 5,000 per month". These four
                    # cover everything in it that carries meaning: the figures,
                    # the currency and the period, which is what made "SGD" and
                    # "month" find a row before the search moved. The connective
                    # words go ("per" no longer matches), and so does the
                    # thousands separator, which the client itself called
                    # locale-fragile — neither is something anyone searches for.
                    cast(Opportunity.salary_min, String).like(like, escape="\\"),
                    cast(Opportunity.salary_max, String).like(like, escape="\\"),
                    func.lower(Opportunity.salary_currency).like(like, escape="\\"),
                    func.lower(Opportunity.salary_period).like(like, escape="\\"),
                )
            )

        total = (
            await session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()

        order_expr = _order_by(sort, descending)

        rows = (
            await session.execute(
                base.order_by(*order_expr).limit(page_limit).offset(offset)
            )
        ).all()

        page_ids = [row[0].id for row in rows]
        evidence = await _evidence_counts(session, page_ids)
        codes = await _decoded_codes(session, page_ids)

    return {
        "items": [
            _payload(
                opportunity,
                internet_id,
                graph_id,
                evidence.get(opportunity.id, (0, 0)),
                codes.get(opportunity.id, []),
            )
            for opportunity, internet_id, graph_id in rows
        ],
        "total": total,
        "limit": page_limit,
        "offset": offset,
        "counts": counts,
    }


def _salary_monthly():
    """`salary_min` normalised to a monthly figure, via `_SALARY_PER_MONTH`.

    Mirrors `monthly()` in job-orders-sort.ts: `salary_min` is what the client
    read (falling back to `salary_max` only when `salary_min` is itself null),
    and an unrecognised or missing period yields NULL rather than assuming
    monthly, so the row sinks instead of landing at a silently wrong position.

    Currency is deliberately ignored, exactly as the client does today: there
    is no FX rate to convert with, and inventing one is the fabrication §15
    forbids.
    """
    amount = func.coalesce(Opportunity.salary_min, Opportunity.salary_max)
    # Matched exactly, which is only safe because the column is now pinned to
    # this vocabulary: `ck_opportunities_salary_period_known` refuses anything
    # else, and `_salary_period` in ingest/persist.py maps what a model answers
    # onto one of these words before the row is written. Before that pair
    # existed this had to lowercase and trim the column, because "Month" went
    # in verbatim and an exact match sank the row.
    factor = case(
        *[
            (Opportunity.salary_period == period, value)
            for period, value in _SALARY_PER_MONTH.items()
        ],
        else_=None,
    )
    return amount / factor


def _quality_rank():
    # `else_=0` rather than NULL, matching the client's `QUALITY_RANK[state] ?? 0`
    # (job-orders-sort.ts:107 before it was deleted): an unranked state sorted
    # as worst, at the top of an ascending list, not sunk to the bottom. It is
    # the coherent reading — a state the code cannot classify is precisely a row
    # that needs a human, which is what the top of this sort is for.
    #
    # Unreachable now that `ck_opportunities_quality_state_known` refuses a
    # fourth state, and kept anyway: `case` needs some else, and an else that
    # agrees with the sort's purpose is better than one that contradicts it.
    return case(
        *[(Opportunity.quality_state == state, rank) for state, rank in _QUALITY_RANK.items()],
        else_=0,
    )


# Column (or expression) each sort key orders by, text columns lowered so the
# order is case-insensitive — "acme" and "Acme" are the same company and an
# uppercase-first ordering would file them pages apart.
_SORT_COLUMN = {
    "received": Opportunity.received_datetime,
    "company": func.lower(Opportunity.company_name_raw),
    "position": func.lower(Opportunity.job_title_raw),
    "salary": _salary_monthly,
    "hours": func.lower(Opportunity.working_hours_raw),
    "duration": func.lower(Opportunity.duration_raw),
    "location": func.lower(Opportunity.location_raw),
    "quality": _quality_rank,
}


def _order_by(sort: str, descending: bool) -> tuple:
    """The ORDER BY clause for a sort key, nulls sinking in both directions.

    SQL does not sink NULLs for you: Postgres puts them first under ASC by
    default, so both directions are spelled out explicitly here rather than
    relying on either default. `id.desc()` breaks ties so paging stays stable
    regardless of which column is being sorted.
    """
    column = _SORT_COLUMN[sort]
    if callable(column):
        column = column()
    ordered = column.desc().nulls_last() if descending else column.asc().nulls_last()
    return (ordered, Opportunity.id.desc())


async def _evidence_counts(session, opportunity_ids: list[uuid.UUID]) -> dict:
    """Verified/total evidence rows for a whole page, in one aggregate query.

    Deliberately a second query keyed on the ids just fetched rather than a
    join on the listing query. Joining would either fan the opportunity rows
    out one per evidence row — turning a page of 50 into hundreds that then
    need collapsing in Python — or force a grouped subquery over the tenant's
    entire evidence table just to read 50 of its groups. Asking after the fact
    touches only the page's rows, and it is one round trip either way; what it
    must never become is one query per row.
    """
    if not opportunity_ids:
        return {}
    result = await session.execute(
        select(
            ExtractionEvidence.opportunity_id,
            # `count(*) FILTER (WHERE …)` rather than two queries or a SUM over
            # a CASE: the filtered count is the thing Postgres is being asked
            # for, and it reads as such.
            func.count().filter(ExtractionEvidence.evidence_valid.is_(True)),
            func.count(),
        )
        .where(ExtractionEvidence.opportunity_id.in_(opportunity_ids))
        .group_by(ExtractionEvidence.opportunity_id)
    )
    return {row[0]: (row[1], row[2]) for row in result}


async def _decoded_codes(session, opportunity_ids: list[uuid.UUID]) -> dict:
    """The shorthand found in each of the page's emails, in one query.

    Same shape and same reason as `_evidence_counts`: keyed on the ids already
    fetched, never one query per row. Joining this onto the listing query would
    fan a page of 50 vacancies out into one row per code and force the collapse
    back together in Python.

    `meaning` is read straight off `opportunity_codes` — the snapshot taken
    when the email was read — and never joined back to `glossary_codes`. An
    agency that corrects its glossary changes what happens next; the list must
    keep showing what the recruiter was actually told at the time, or the
    interpretation on screen stops being the one that was applied.

    Ordered by position so the codes read in the order the client wrote them.
    """
    if not opportunity_ids:
        return {}
    result = await session.execute(
        select(OpportunityCode)
        .where(OpportunityCode.opportunity_id.in_(opportunity_ids))
        .order_by(OpportunityCode.opportunity_id, OpportunityCode.start_char)
    )
    grouped: dict[uuid.UUID, list[OpportunityCode]] = {}
    for code in result.scalars():
        grouped.setdefault(code.opportunity_id, []).append(code)
    return grouped


@router.post("/opportunities/{opportunity_id}/review")
async def set_review_status(
    opportunity_id: uuid.UUID, body: ReviewRequest, request: Request
) -> dict:
    """Mark a vacancy reviewed, or put it back.

    Un-reviewing lands on `ready`, never back on `needs_review`, even for a row
    the pipeline flagged. Once a human has looked at it the machine's doubt is
    stale, and restoring it would put the row back in a queue its own reviewer
    had already cleared.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    new_status = _REVIEWED if body.reviewed else _READY

    async with tenant_session(tenant_uuid) as session:
        # RLS keeps this inside the agency; it says nothing about which
        # recruiter inside that agency may change the row, which is what
        # `load_editable_opportunity` decides.
        await load_editable_opportunity(session, opportunity_id, user_uuid, role)
        updated = (
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == opportunity_id)
                .values(review_status=new_status)
                .returning(Opportunity.review_status)
            )
        ).scalar_one_or_none()

    if updated is None:
        # 404 rather than 403 for a row belonging to another agency: the two
        # answers are indistinguishable to this code by design, and 403 would
        # confirm the id exists somewhere, which is itself a leak.
        raise HTTPException(status_code=404, detail="No such job order.")

    return {
        "id": str(opportunity_id),
        "review_status": _STORED_TO_FILTER.get(updated, updated),
    }


@router.post("/opportunities/{opportunity_id}/placement-type")
async def set_placement_type(
    opportunity_id: uuid.UUID, body: PlacementTypeRequest, request: Request
) -> dict:
    """Set what kind of placement this vacancy is — a human decision, never an
    inference (see the column comment in `app/models/opportunity.py`)."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        # The name written into `placement_type_set_by` goes against a
        # regulatory decision, so it must belong to someone actually given
        # the job order — not merely someone it was shared with.
        await load_editable_opportunity(session, opportunity_id, user_uuid, role)
        # `id` alongside `placement_type` in RETURNING: `RETURNING` gives back
        # exactly what was written, which is ambiguous when the write itself
        # is NULL — clearing an already-NULL placement_type on a real row
        # looks identical to matching no row at all. Returning `id` too tells
        # the two apart without a second round trip.
        #
        # `placement_type_set_by`/`_set_at` are written on every call,
        # including a clear back to NULL — the CHECK stays the source of
        # truth for what "set" is, but who most recently touched the field
        # is worth recording even for a clear.
        row = (
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == opportunity_id)
                .values(
                    placement_type=body.placement_type,
                    placement_type_set_by=user_uuid,
                    placement_type_set_at=func.now(),
                )
                .returning(Opportunity.id, Opportunity.placement_type)
            )
        ).one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="No such job order.")

    return {"id": str(opportunity_id), "placement_type": row[1]}


@router.post("/opportunities/{opportunity_id}/occupational-requirement")
async def set_occupational_requirement(
    opportunity_id: uuid.UUID, body: OccupationalRequirementRequest, request: Request
) -> dict:
    """Set (or clear) the job's own genuine occupational sex requirement.

    The pairing rule is enforced twice: `OccupationalRequirementRequest`
    refuses a requirement with no reason before this ever reaches the
    database, and `ck_opportunities_sex_requirement_has_reason` refuses it
    again for any row a script or a future endpoint writes directly.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        # Same reason as `placement-type`: `sex_requirement_set_by` records a
        # lawful judgement, and a share is not the authority to make one.
        await load_editable_opportunity(session, opportunity_id, user_uuid, role)
        updated = (
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == opportunity_id)
                .values(
                    sex_requirement=body.sex_requirement,
                    sex_requirement_reason=(
                        body.sex_requirement_reason.strip()
                        if body.sex_requirement_reason
                        else None
                    ),
                    sex_requirement_set_by=user_uuid,
                    sex_requirement_set_at=func.now(),
                )
                .returning(Opportunity.sex_requirement, Opportunity.sex_requirement_reason)
            )
        ).one_or_none()

    if updated is None:
        raise HTTPException(status_code=404, detail="No such job order.")

    return {
        "id": str(opportunity_id),
        "sex_requirement": updated[0],
        "sex_requirement_reason": updated[1],
    }


# allow-hardcode: `description=` below is FastAPI/OpenAPI docs prose — human
# copy shown to a developer reading /docs, not matching or scoring logic.
@router.get("/opportunities/{opportunity_id}/candidates/{candidate_id}/eligibility")
async def get_eligibility(
    opportunity_id: uuid.UUID,
    candidate_id: uuid.UUID,
    request: Request,
    # `Annotated`, not a `Query(...)` default: a call sitting in the default
    # slot is what B008 exists to catch, since a mutable default is built
    # once at import and shared across every request.
    as_of: Annotated[
        date | None,
        Query(
            description=(
                "Judge age as of this date rather than today. MOM judges age "
                "at application, not at browse time, so a recruiter planning "
                "ahead can ask the question as of the date the permit would "
                "actually be filed."
            )
        ),
    ] = None,
) -> dict:
    """Whether `candidate_id` may be legitimately narrowed for `opportunity_id`
    (§15). Reports; does not filter — the caller decides what to show, and
    nothing here is hidden. A candidate missing a fact appears with an
    `unknown` finding rather than being silently absent from the list, and a
    criterion a placement type does not govern appears `not_applicable`
    rather than being omitted.

    Deliberately no `eligible: true/false` summary field, and none should be
    added: any two-valued rollup collapses `unknown` and `not_applicable`
    into whichever bucket someone picked, which erases the exact distinction
    this endpoint exists to surface. See `app/services/sourcing/eligibility.py`
    and `tests/test_eligibility.py::test_no_boolean_rollup_in_response`.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    evaluated_as_of = as_of or date.today()

    async with tenant_session(tenant_uuid) as session:
        opportunity = await load_visible_opportunity(
            session, opportunity_id, user_uuid, role
        )

        candidate = (
            await session.execute(select(Candidate).where(Candidate.id == candidate_id))
        ).scalar_one_or_none()
        if candidate is None:
            raise HTTPException(status_code=404, detail="No such candidate.")

    if opportunity.placement_type is None:
        # The job order has not been classified yet — a different thing from
        # every candidate passing, so the caller must be able to say so rather
        # than rendering an empty list as though nothing were wrong.
        return {
            "placement_type": None,
            "assessable": False,
            "evaluated_as_of": evaluated_as_of.isoformat(),
            "findings": [],
        }

    facts = eligibility.CandidateFacts(
        sex=candidate.sex,
        date_of_birth=candidate.date_of_birth,
        education_years=candidate.education_years,
        nationality=candidate.nationality,
    )
    findings = eligibility.evaluate(
        opportunity.placement_type,
        facts,
        as_of=evaluated_as_of,
        min_age_years=settings.MDW_MIN_AGE_YEARS,
        max_age_years_exclusive=settings.MDW_MAX_AGE_YEARS_EXCLUSIVE,
        min_education_years=settings.MDW_MIN_EDUCATION_YEARS,
        approved_source_countries=settings.MDW_APPROVED_SOURCE_COUNTRIES,
        sex_requirement=opportunity.sex_requirement,
        sex_requirement_reason=opportunity.sex_requirement_reason,
    )

    return {
        "placement_type": opportunity.placement_type,
        "assessable": True,
        "evaluated_as_of": evaluated_as_of.isoformat(),
        "findings": [
            {
                "criterion": f.criterion,
                "outcome": f.outcome,
                "detail": f.detail,
                "basis": f.basis,
            }
            for f in findings
        ],
    }


def _payload(
    row: Opportunity,
    internet_message_id: str | None,
    graph_message_id: str | None,
    evidence: tuple[int, int],
    codes: list[OpportunityCode],
) -> dict:
    """One row, with absences preserved as absences."""
    verified_fields, total_fields = evidence
    return {
        # The decoded shorthand, with the offsets that make it checkable: the
        # client wrote `C/F` at 142–145 and the agency's own glossary says that
        # means Chinese female. Both halves travel together because either one
        # alone is an assertion — the offsets without the meaning are unreadable
        # and the meaning without the offsets is unverifiable.
        "codes": [
            {
                "code": code.code,
                "meaning": code.meaning,
                "attribute": code.attribute,
                "start_char": code.start_char,
                "end_char": code.end_char,
            }
            for code in codes
        ],
        # Derived here, not in the browser. This flag is what the feature is
        # for — it says a client's request touched a protected characteristic —
        # and a rule the client re-implements is a rule that can quietly differ
        # per client, on exactly the rows where being wrong matters most.
        "references_protected_attribute": any(c.attribute is not None for c in codes),
        "id": str(row.id),
        "internet_message_id": internet_message_id,
        "graph_message_id": graph_message_id,
        # What replaces the confidence percentage the UI used to show. A
        # calibrated probability is not what a recruiter needs to decide
        # whether to trust a row — "6 of 8 values were found verbatim in the
        # email" is checkable, and `model_confidence` (see
        # `models/extraction.py`) is never shown as a probability.
        "verified_fields": verified_fields,
        "total_fields": total_fields,
        "received_datetime": (
            row.received_datetime.isoformat() if row.received_datetime else None
        ),
        "company_name_raw": row.company_name_raw,
        "job_title_raw": row.job_title_raw,
        "salary_raw": row.salary_raw,
        # Numeric comes back as Decimal, which is not JSON. Converted here
        # rather than left to the encoder so the boundary is explicit — and
        # `is None` rather than a truthiness test, because a genuine 0 is a
        # value someone extracted and must not be flattened into "missing".
        "salary_min": None if row.salary_min is None else float(row.salary_min),
        "salary_max": None if row.salary_max is None else float(row.salary_max),
        "salary_currency": row.salary_currency,
        "salary_period": row.salary_period,
        "working_hours_raw": row.working_hours_raw,
        "requirements": row.requirements,
        # The body of the posting, not a summary of it. Withheld from the
        # payload the table renders, a recruiter has to open the email to
        # answer "what is the job", which is the question the whole row is
        # about — and it is the field the free-text search is least able to
        # do without.
        "job_description": row.job_description,
        "duration_raw": row.duration_raw,
        "location_raw": row.location_raw,
        "quality_state": row.quality_state,
        # Translated, not passed through: the chips, the `status` parameter and
        # this field have to be the same vocabulary or a client cannot filter
        # on what it just rendered. `ready` becomes `new`; anything the
        # pipeline invents later passes through unrenamed rather than being
        # forced into a bucket it does not belong in.
        "review_status": _STORED_TO_FILTER.get(row.review_status, row.review_status),
    }
