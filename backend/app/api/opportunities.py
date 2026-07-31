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
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, model_validator
from sqlalchemy import String, and_, case, cast, func, or_, select, update
from sqlalchemy import true as true_
from sqlalchemy.orm import aliased

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.db.rls import tenant_session
from app.models import (
    Candidate,
    Client,
    EmailMessage,
    Opportunity,
    OpportunityCode,
    User,
)
from app.models.extraction import ExtractionEvidence
from app.services.notify.dispatch import emit_and_enqueue
from app.services.notify.events import (
    EVENT_OPPORTUNITY_ASSIGNED,
    EVENT_OPPORTUNITY_NEW,
    OpportunityEvent,
)
from app.services.sourcing import eligibility
from app.services.visibility import (
    can_edit,
    load_editable_opportunity,
    load_visible_opportunity,
    shared_with_me_exists,
    visible_opportunities,
)

router = APIRouter(tags=["opportunities"])


def _assignee_name_expr(user: Any) -> Any:
    """How a colleague is named, wherever this module names one.

    NULLIF over each candidate rather than a bare COALESCE, and the email's
    LOCAL PART as the last resort — both exactly as
    `app/api/clients.py::_assignee_name_expr` and `GET /api/members` do it. A
    nameless colleague must be "raj" on every screen; "raj@agency.sg" here and
    "raj" in the sharing picker reads as two different people.

    Takes the entity (an alias or the mapped class) so the list's join and a
    single-row lookup can share one definition instead of drifting apart.
    """
    return func.coalesce(
        func.nullif(func.btrim(user.preferred_name), ""),
        func.nullif(func.btrim(user.display_name), ""),
        func.split_part(user.email, "@", 1),
    )

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

ScopeFilter = Literal["mine", "queue", "shared_with_me", "all"]

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


def _scope_clause(scope: str, user_id: uuid.UUID):
    """A filter WITHIN what `visible_opportunities` already allows, never a
    widening of it. It is ANDed with the predicate, never substituted for it.
    """
    if scope == "mine":
        return Opportunity.assigned_user_id == user_id
    if scope == "queue":
        return Opportunity.assigned_user_id.is_(None)
    if scope == "shared_with_me":
        return shared_with_me_exists(user_id)
    return true_()


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
    scope: ScopeFilter = "all",
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
    scope_clause = _scope_clause(scope, user_uuid)

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
            .where(scope_clause)
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
        assignee = aliased(User)
        linked_client = aliased(Client)
        shared_with_me_expr = shared_with_me_exists(user_uuid)
        base = (
            select(
                Opportunity,
                email.internet_message_id,
                email.graph_message_id,
                _assignee_name_expr(assignee).label("assignee_name"),
                linked_client.name.label("client_name"),
                shared_with_me_expr.label("shared_with_me"),
            )
            # OUTER, and that matters: `email_message_id` is nullable — a job
            # order typed in by hand has no email at all, and a retention purge
            # sets the column NULL on one that did. An inner join drops both
            # from the list while the chip counts above (which do not join)
            # still count them, so the page would say twelve and show eleven.
            .join(email, email.id == Opportunity.email_message_id, isouter=True)
            # Composite, not just `id`: every user reference in this codebase
            # carries the tenant predicate alongside the id so tenant safety
            # never rests on RLS alone (see the composite FKs on
            # clients.assigned_user_id, opportunities.assigned_user_id and
            # opportunity_shares.shared_with_user_id). OUTER — an inner join
            # would drop every unassigned job order, the entire queue.
            .join(
                assignee,
                and_(
                    assignee.id == Opportunity.assigned_user_id,
                    assignee.tenant_id == Opportunity.tenant_id,
                ),
                isouter=True,
            )
            # The client the job order is filed under, resolved here rather
            # than a lookup per row. Composite and OUTER for the same two
            # reasons as the assignee join above — and OUTER matters more
            # here: most job orders are not linked to a client at all (eight
            # of eleven in production when this was written), so an inner join
            # would drop the majority of the list.
            .join(
                linked_client,
                and_(
                    linked_client.id == Opportunity.client_id,
                    linked_client.tenant_id == Opportunity.tenant_id,
                ),
                isouter=True,
            )
            .where(visible)
            .where(scope_clause)
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
                assignee_name,
                client_name,
                shared_with_me,
            )
            for (
                opportunity,
                internet_id,
                graph_id,
                assignee_name,
                client_name,
                shared_with_me,
            ) in rows
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
    assignee_name: str | None,
    client_name: str | None,
    shared_with_me: bool,
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
        "assigned_user_id": str(row.assigned_user_id) if row.assigned_user_id else None,
        # Denormalised rather than resolved in the browser against the members
        # list: otherwise every row's rendering depends on a second request
        # having already landed, which is a race the list does not otherwise
        # have.
        "assignee_name": assignee_name,
        "client_id": str(row.client_id) if row.client_id else None,
        # Denormalised for the same reason as `assignee_name`, and for one
        # more: the id alone is unreadable, so a recruiter who has just filed
        # eight job orders under six different companies has no way to check
        # what they chose.
        "client_name": client_name,
        "source": row.source,
        # "A share is one of the reasons you can see this", not "the only
        # reason" — see the design note on why the two differ.
        "shared_with_me": shared_with_me,
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


class AssignRequest(BaseModel):
    """`null` is a value here, not an omission — it releases the job order back
    to the queue, which is a different act from "leave it where it is"."""

    user_id: uuid.UUID | None = None


class ClientLinkRequest(BaseModel):
    client_id: uuid.UUID | None
    # Defaults true: linking a client should produce the same outcome the
    # pipeline would have produced had the link been there from the start —
    # a job order goes to the client's recruiter. It stays a flag because
    # linking a client and taking ownership are two different intentions.
    adopt_client_recruiter: bool = True


class ManualOpportunityRequest(BaseModel):
    """A vacancy taken over the phone or on WhatsApp and typed in.

    Every field is optional and every one is a `_raw` string, deliberately: the
    recruiter is transcribing what they were told, and forcing a structured
    salary out of "6k neg." would be the fabrication §15 forbids. Normalisation
    is the extraction pipeline's job and is simply absent here.
    """

    client_id: uuid.UUID | None = None
    company_name_raw: str | None = None
    job_title_raw: str | None = None
    location_raw: str | None = None
    salary_raw: str | None = None
    working_hours_raw: str | None = None
    duration_raw: str | None = None
    employment_type: str | None = None
    job_description: str | None = None
    requirements: str | None = None


@router.post("/opportunities/{opportunity_id}/claim")
async def claim_opportunity(opportunity_id: uuid.UUID, request: Request) -> dict:
    """Take an unassigned job order.

    The `WHERE assigned_user_id IS NULL` is the whole concurrency story: two
    recruiters pressing this at the same moment both run the same UPDATE, and
    exactly one of them matches a row. A SELECT to check first and an UPDATE
    to act would reintroduce precisely the gap this clause closes — the second
    reader would see NULL too, and both would be told they had won.

    Deliberately not behind `load_editable_opportunity`, and
    `tests/test_opportunity_routes_guarded.py` exempts it for that reason:
    `can_edit` refuses an unassigned job order on purpose, and an unassigned
    job order is the only kind this route acts on. Claiming is the act that
    makes it editable.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        # Visible, so an invisible id is 404 rather than 409 — a 409 would
        # confirm the row exists to someone with no business knowing it does.
        await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        claimed = (
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == opportunity_id)
                .where(Opportunity.assigned_user_id.is_(None))
                .values(assigned_user_id=user_uuid)
                .returning(Opportunity.id)
            )
        ).scalar_one_or_none()

    if claimed is None:
        raise HTTPException(
            status_code=409, detail="Someone else has taken this job order."
        )

    # Nothing is emitted. You pressed the button; being told what you just did
    # is noise, and the colleagues who lost the race learn it from their 409.
    return {"id": str(opportunity_id), "assigned_user_id": str(user_uuid)}


@router.post("/opportunities/{opportunity_id}/assign")
async def assign_opportunity(
    opportunity_id: uuid.UUID, body: AssignRequest, request: Request
) -> dict:
    """Hand a job order to a colleague, or put it back on the queue.

    Guarded by its own rule rather than by `load_editable_opportunity` alone:
    it needs edit rights (so an ordinary recruiter cannot pass on someone
    else's work), and it must also refuse a target from outside the agency
    before the composite FK turns that into a 500.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        opportunity = await load_editable_opportunity(
            session, opportunity_id, user_uuid, role
        )
        # Read off the row while the session is open: the notification is sent
        # after commit, by which point a committed instance's attributes are
        # expired. Same ordering argument as `opportunity_shares.py`.
        subject = (
            opportunity.job_title_raw,
            opportunity.company_name_raw,
            opportunity.location_raw,
            opportunity.salary_raw,
        )

        if body.user_id is not None:
            # RLS scopes this to the agency, so a colleague of another agency
            # is simply not found.
            colleague = (
                await session.execute(
                    select(User.id).where(User.id == body.user_id)
                )
            ).scalar_one_or_none()
            if colleague is None:
                raise HTTPException(
                    status_code=422, detail="That colleague is not in this agency."
                )

        await session.execute(
            update(Opportunity)
            .where(Opportunity.id == opportunity_id)
            .values(assigned_user_id=body.user_id)
        )

        actor = (
            await session.execute(
                select(User.display_name).where(User.id == user_uuid)
            )
        ).scalar_one_or_none()

    job_title, company_name, location, salary = subject
    if body.user_id is None:
        # Released. `EVENT_OPPORTUNITY_NEW` with `recipient_user_ids=None` —
        # None means everybody, and this is queue work again that nobody would
        # otherwise learn is available. An empty tuple would mean the tenant's
        # shared destinations and no individual at all, which is the opposite.
        event = OpportunityEvent(
            kind=EVENT_OPPORTUNITY_NEW,
            tenant_id=tenant_uuid,
            opportunity_id=opportunity_id,
            recipient_user_ids=None,
            job_title=job_title,
            company_name=company_name,
            location=location,
            salary=salary,
            actor_name=actor,
        )
    elif body.user_id == user_uuid:
        # Assigning it to yourself is the claim case: you know.
        event = None
    else:
        event = OpportunityEvent(
            kind=EVENT_OPPORTUNITY_ASSIGNED,
            tenant_id=tenant_uuid,
            opportunity_id=opportunity_id,
            # Only them. This concerns one person, and telling the agency who
            # got which job order is a stream nobody asked for.
            recipient_user_ids=(body.user_id,),
            job_title=job_title,
            company_name=company_name,
            location=location,
            salary=salary,
            actor_name=actor,
        )

    # After commit, like `opportunity_shares.py`: the row must be assigned
    # before anyone is told it was. The same non-atomicity is accepted for the
    # same reason — a missed notification beats a notification about an
    # assignment that was rolled back.
    if event is not None:
        await emit_and_enqueue(event)

    return {
        "id": str(opportunity_id),
        "assigned_user_id": str(body.user_id) if body.user_id else None,
    }


@router.post("/opportunities/{opportunity_id}/client")
async def set_opportunity_client(
    opportunity_id: uuid.UUID, body: ClientLinkRequest, request: Request
) -> dict:
    """Say which client this job order came from.

    Guarded by "editable OR unassigned", not by `load_editable_opportunity`.
    `can_edit` refuses unassigned rows on purpose — claiming is what makes a
    job order editable — but recording which company a job order came from is
    a factual correction rather than an act of ownership, and the queue is
    shared work. Someone else's *assigned* job order stays closed.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        opportunity = await load_visible_opportunity(
            session, opportunity_id, user_uuid, role
        )
        if not (
            opportunity.assigned_user_id is None
            or can_edit(opportunity, user_uuid, role)
        ):
            raise HTTPException(
                status_code=403,
                detail="This job order is shared with you, not assigned to you.",
            )

        # Read off the row while the session is open: after commit a
        # committed instance's attributes are expired, and the event below
        # needs these. Same ordering argument as `assign_opportunity`.
        previous_assignee = opportunity.assigned_user_id
        subject_title = opportunity.job_title_raw
        subject_company = opportunity.company_name_raw
        subject_location = opportunity.location_raw
        subject_salary = opportunity.salary_raw

        adopted: uuid.UUID | None = opportunity.assigned_user_id
        client_name: str | None = None
        if body.client_id is not None:
            # RLS scopes this to the agency, so a client of another agency is
            # simply not found — refused here rather than as a 500 from the
            # composite foreign key.
            client = (
                await session.execute(
                    select(Client.id, Client.name, Client.assigned_user_id).where(
                        Client.id == body.client_id
                    )
                )
            ).one_or_none()
            if client is None:
                raise HTTPException(
                    status_code=422, detail="That client is not in this agency."
                )
            # Named in the response for the same reason `assignee_name` is:
            # the browser sent an id and holds no name, so without this the
            # picker goes empty again the moment the link succeeds.
            client_name = client.name
            # Only an unassigned job order adopts. An assigned one never
            # changes hands here — that is what the assign route is for.
            if (
                body.adopt_client_recruiter
                and opportunity.assigned_user_id is None
                and client.assigned_user_id is not None
            ):
                adopted = client.assigned_user_id

        await session.execute(
            update(Opportunity)
            .where(Opportunity.id == opportunity_id)
            .values(client_id=body.client_id, assigned_user_id=adopted)
        )

        name = None
        if adopted is not None:
            name = (
                await session.execute(
                    select(_assignee_name_expr(User)).where(User.id == adopted)
                )
            ).scalar_one_or_none()

    # A job order quietly becoming yours is exactly what the assigned event
    # exists to announce. `assign_opportunity` emits it; this route assigns
    # too, so it must as well, or adoption is the one way to gain work without
    # being told. Emitted after the transaction commits, matching the ordering
    # `opportunity_shares.py` uses and explains.
    if adopted is not None and adopted != previous_assignee:
        await emit_and_enqueue(
            OpportunityEvent(
                kind=EVENT_OPPORTUNITY_ASSIGNED,
                tenant_id=tenant_uuid,
                opportunity_id=opportunity_id,
                recipient_user_ids=(adopted,),
                job_title=subject_title,
                company_name=subject_company,
                location=subject_location,
                salary=subject_salary,
            )
        )

    return {
        "id": str(opportunity_id),
        "client_id": str(body.client_id) if body.client_id else None,
        "client_name": client_name,
        "assigned_user_id": str(adopted) if adopted else None,
        "assignee_name": name,
    }


@router.post("/opportunities", status_code=201)
async def create_opportunity(body: ManualOpportunityRequest, request: Request) -> dict:
    """A vacancy that never arrived as an email.

    `assigned_user_id` is the creator, not the client's assignee: a recruiter
    who takes a job order over the phone is the one working it, and silently
    handing it to whoever happens to own the client would lose it from the
    list of the person who just typed it.

    `source` is `manual` and `email_message_id` is NULL — the pair is not
    redundant. `email_message_id` is ON DELETE SET NULL, so a retention purge
    would otherwise reclassify an extracted job order as hand-typed.
    """
    user_uuid, tenant_uuid, _role = await _require_session_with_role(request)

    opportunity_id = uuid.uuid4()
    async with tenant_session(tenant_uuid) as session:
        session.add(
            Opportunity(
                id=opportunity_id,
                tenant_id=tenant_uuid,
                email_message_id=None,
                # When it was taken down, which is the closest honest answer to
                # "when did this arrive" and keeps it sorting beside the
                # extracted rows instead of sinking under every NULL.
                received_datetime=datetime.now(UTC),
                client_id=body.client_id,
                assigned_user_id=user_uuid,
                source=Opportunity.MANUAL,
                company_name_raw=body.company_name_raw,
                job_title_raw=body.job_title_raw,
                location_raw=body.location_raw,
                salary_raw=body.salary_raw,
                working_hours_raw=body.working_hours_raw,
                duration_raw=body.duration_raw,
                employment_type=body.employment_type,
                job_description=body.job_description,
                requirements=body.requirements,
                # A person typed this; there is no extraction to doubt and no
                # evidence to verify, so it is neither `needs_review` nor
                # `verified`.
                review_status=_READY,
                quality_state="likely",
            )
        )

    # No notification: it is already assigned to whoever created it, and they
    # are the only person it concerns.
    return {
        "id": str(opportunity_id),
        "source": Opportunity.MANUAL,
        "assigned_user_id": str(user_uuid),
        "client_id": str(body.client_id) if body.client_id else None,
        "company_name_raw": body.company_name_raw,
        "job_title_raw": body.job_title_raw,
    }
