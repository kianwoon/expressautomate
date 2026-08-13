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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, model_validator
from sqlalchemy import (
    ForeignKeyConstraint,
    String,
    and_,
    case,
    cast,
    func,
    or_,
    select,
    update,
)
from sqlalchemy import true as true_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.db.rls import tenant_session
from app.models import (
    Buddy,
    BuddyReferral,
    Client,
    EmailMessage,
    Opportunity,
    OpportunityCode,
    OpportunityDocument,
    User,
)
from app.models.extraction import ExtractionEvidence
from app.services.client_matching import resolve_or_create_client_by_name
from app.services.notify.dispatch import emit_and_enqueue
from app.services.notify.events import (
    EVENT_OPPORTUNITY_ASSIGNED,
    EVENT_OPPORTUNITY_NEW,
    OpportunityEvent,
)
from app.services.opportunity_dedupe import duplicate_opportunity_ids
from app.services.sourcing import eligibility
from app.services.visibility import (
    can_edit,
    load_editable_opportunity,
    load_visible_candidate,
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


def _row_select(user_uuid: uuid.UUID):
    """Everything one rendered job order is made of, in one select.

    Shared by the list and by `get_opportunity` rather than written twice, and
    that is the point: the panel replaces a list row with what the single-row
    read returns, so two selects that drift by one join produce a row that
    changes appearance the moment it is re-read.

    `email_messages` carries the two message ids, so this joins it rather than
    denormalising them onto the opportunity: they exist to let a recruiter open
    the original mail, and a copy that drifts from the source is worse than a
    join.
    """
    email = aliased(EmailMessage)
    assignee = aliased(User)
    linked_client = aliased(Client)
    buddy = aliased(Buddy)
    buddy_referral = aliased(BuddyReferral)
    # The row this one replaced, if this is a later revision of an open job
    # order whose requirements the client changed. A correlated subquery rather
    # than a join because the link is the *reverse* of
    # `superseded_by_opportunity_id` (predecessor -> successor) and the list is
    # read row by row. NULL for every ordinary job order — the common case.
    predecessor = aliased(Opportunity)
    revision_of = (
        select(predecessor.id)
        .where(predecessor.superseded_by_opportunity_id == Opportunity.id)
        .correlate(Opportunity)
        .scalar_subquery()
        .label("revision_of_opportunity_id")
    )
    # The buddy who referred this client — the person who actually owns the
    # account — resolved as a *correlated* subquery rather than a join. A
    # client can be referred by more than one buddy
    # (`uq_buddy_referrals_once` is per buddy+client, not per client), and a
    # plain join on `client_id` returns one row per referring buddy: every job
    # order linked to that client then appears once per buddy. Folding two
    # identical rows into the page was the duplicate "Operations Assistant
    # (Driver)" complaint. A LATERAL that picks one referral (the earliest)
    # keeps it to one row per job order, so the buddy still shows as Owner
    # without multiplying the list.
    one_buddy = (
        select(buddy.name.label("name"))
        .select_from(buddy_referral)
        .join(
            buddy,
            and_(
                buddy.id == buddy_referral.buddy_id,
                buddy.tenant_id == buddy_referral.tenant_id,
            ),
        )
        .where(
            buddy_referral.client_id == Opportunity.client_id,
            buddy_referral.tenant_id == Opportunity.tenant_id,
        )
        # Deterministic across re-runs, so the same row re-read after a write
        # shows the same buddy rather than whatever row order Postgres chose.
        # `created_at` is the referral that established the link first; the id
        # breaks a tie without depending on a name a buddy could later change.
        .order_by(buddy_referral.created_at.asc(), buddy_referral.id.asc())
        .limit(1)
        .lateral("one_buddy")
    )
    return (
        select(
            Opportunity,
            email.internet_message_id,
            email.graph_message_id,
            _assignee_name_expr(assignee).label("assignee_name"),
            linked_client.name.label("client_name"),
            shared_with_me_exists(user_uuid).label("shared_with_me"),
            one_buddy.c.name.label("buddy_name"),
            revision_of,
        )
        # OUTER, and that matters: `email_message_id` is nullable — a job
        # order typed in by hand has no email at all, and a retention purge
        # sets the column NULL on one that did. An inner join drops both
        # from the list while the chip counts (which do not join)
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
        # The one-buddy LATERAL above, joined OUTER so clients with no
        # referral (the common case) are not dropped.
        .join(one_buddy, true_(), isouter=True)
    )


async def _duplicate_opportunity_ids(session, visible) -> set[uuid.UUID]:
    """The ids of job orders that are a later re-forward of an open job already held.

    Delegates to the shared `opportunity_dedupe` service so the job orders list
    and the buddies referral count agree on what a duplicate is.
    """
    dupes = duplicate_opportunity_ids(visible)
    rows = (await session.execute(select(dupes.c.id))).all()
    return {row[0] for row in rows}


async def _current_revision_id(session, opportunity_id: uuid.UUID) -> uuid.UUID:
    """The id at the end of the supersede chain, without a visibility check.

    Only `claim_opportunity` uses this. Every other route resolves the chain
    through `load_visible_opportunity`, which applies the per-recruiter
    predicate; claim cannot, because its whole design is compare-and-set first,
    visibility after — the loser of a race must get the honest 409, not a 404
    about a row that is merely no longer unassigned. Following ids alone is
    safe here: RLS confines every hop to the tenant, and the claim's UPDATE can
    only match an unassigned row, which is visible to the entire agency anyway.
    """
    current = opportunity_id
    seen: set[uuid.UUID] = set()
    while True:
        seen.add(current)
        superseded_by = (
            await session.execute(
                select(Opportunity.superseded_by_opportunity_id).where(
                    Opportunity.id == current
                )
            )
        ).scalar_one_or_none()
        if superseded_by is None or superseded_by in seen:
            return current
        current = superseded_by


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
    dedupe: bool = True,
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
        # Hide later re-forwards of job orders already held open. Computed
        # before the chip counts so the counts and the list agree: a recruiter
        # who sees "96" in the chips and 86 rows on screen reads the gap as a
        # bug, and with dedupe on by default it would be the first thing they
        # see. The count hidden is returned so it is visible, not silent.
        hidden = 0
        dupe_ids: set[uuid.UUID] = set()
        if dedupe:
            dupe_ids = await _duplicate_opportunity_ids(session, visible)
            hidden = len(dupe_ids)
        dedupe_clause = (
            Opportunity.id.not_in(dupe_ids) if dupe_ids else None
        )

        # The chips are counted over everything visible to this caller, in
        # their own query,
        # before any filter or window is applied. A count that moved with the
        # page would tell the recruiter there are 12 vacancies needing review
        # on page one and 3 on page two, which is not a smaller truth — it is
        # a different question than the one the chip appears to answer.
        counts = {name: 0 for name in _FILTER_TO_STORED}
        counts["all"] = 0
        count_q = (
            select(Opportunity.review_status, func.count())
            .where(visible)
            .where(scope_clause)
        )
        if dedupe_clause is not None:
            count_q = count_q.where(dedupe_clause)
        for stored, n in await session.execute(
            count_q.group_by(Opportunity.review_status)
        ):
            counts["all"] += n
            # A stored value this API has no name for (a future state written
            # by the pipeline) still counts towards `all` but is not invented
            # as a chip. Silently dropping it from `all` would make the totals
            # disagree with the list, which is the harder bug to see.
            if stored in _STORED_TO_FILTER:
                counts[_STORED_TO_FILTER[stored]] += n

        base = _row_select(user_uuid).where(visible).where(scope_clause)
        if status is not None:
            base = base.where(Opportunity.review_status == _FILTER_TO_STORED[status])

        if dedupe_clause is not None:
            base = base.where(dedupe_clause)

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
        documents = await _documents_for(session, page_ids)

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
                buddy_name,
                documents=documents.get(opportunity.id, []),
                revision_of_opportunity_id=revision_of,
            )
            for (
                opportunity,
                internet_id,
                graph_id,
                assignee_name,
                client_name,
                shared_with_me,
                buddy_name,
                revision_of,
            ) in rows
        ],
        "total": total,
        "limit": page_limit,
        "offset": offset,
        "counts": counts,
        # How many later re-forwards the dedupe filter hid, so the list can say
        # "N duplicates hidden" rather than silently dropping rows. Zero when
        # the filter is off.
        "hidden": hidden,
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


async def _documents_for(
    session, opportunity_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict]]:
    """The job-description files attached to each of the page's rows, in one query.

    Same shape and same reason as `_evidence_counts`: keyed on the ids already
    fetched, never one query per row. A page of 50 vacancies with one document
    each must not become 50 queries.

    Serialized here (rather than joining and serializing in the payload loop)
    because the loop has the rows, not the documents; the shape mirrors
    `opportunity_documents.serialize` minus `prefill`, which the list and the
    detail panel never render — the create-dialog's own poll reads the full row
    from the dedicated route. Keeping the extraction JSON out of every list
    payload is the difference between a lean page and a page carrying an
    unseen copy of every attached document's model output.
    """
    if not opportunity_ids:
        return {}
    result = await session.execute(
        select(OpportunityDocument)
        .where(OpportunityDocument.opportunity_id.in_(opportunity_ids))
        .order_by(OpportunityDocument.created_at.asc(), OpportunityDocument.id.asc())
    )
    grouped: dict[uuid.UUID, list[dict]] = {}
    for document in result.scalars():
        grouped.setdefault(document.opportunity_id, []).append(
            {
                "id": str(document.id),
                "filename": document.filename,
                "content_type": document.content_type,
                "byte_size": document.byte_size,
                "extract_state": document.extract_state,
                "extract_error": document.extract_error,
                "created_at": (
                    document.created_at.isoformat() if document.created_at else None
                ),
            }
        )
    return grouped


@router.get("/opportunities/{opportunity_id}")
async def get_opportunity(opportunity_id: uuid.UUID, request: Request) -> dict:
    """One job order, in exactly the shape the list gives one.

    Every write in the panel answers with only the fields it changed, so the
    panel re-reads the row afterwards and swaps it into the list. Without this
    the re-read 404s, the browser swallows the failure, and the screen catches
    up only on the next poll — which is what production did.

    Not visible is 404, never 403: a 403 would confirm the row exists, and
    whether another agency holds a given id is not this route's to tell.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        # Resolves supersede chains: the panel for a job order revised by a
        # later email must show the current revision's requirements, not the
        # stale row the client replaced. The row select below keys on the
        # resolved id, or the loader and the read would disagree about which
        # job order this is.
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        row = (
            await session.execute(
                _row_select(user_uuid).where(Opportunity.id == current.id)
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="No such job order.")
        evidence = await _evidence_counts(session, [current.id])
        codes = await _decoded_codes(session, [current.id])
        documents = await _documents_for(session, [current.id])

    (
        opportunity,
        internet_id,
        graph_id,
        assignee_name,
        client_name,
        shared,
        buddy_name,
        revision_of,
    ) = row
    return _payload(
        opportunity,
        internet_id,
        graph_id,
        evidence.get(opportunity.id, (0, 0)),
        codes.get(opportunity.id, []),
        assignee_name,
        client_name,
        shared,
        buddy_name,
        documents=documents.get(opportunity.id, []),
        revision_of_opportunity_id=revision_of,
    )


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
        # `load_editable_opportunity` decides. It also resolves supersede
        # chains, so the write lands on the *current* revision — an edit aimed
        # at a stale id is an edit of the job order, not of its history.
        current = await load_editable_opportunity(session, opportunity_id, user_uuid, role)
        updated = (
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == current.id)
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
        "id": str(current.id),
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
        # the job order — not merely someone it was shared with. Resolves
        # supersede chains, so the write lands on the current revision.
        current = await load_editable_opportunity(session, opportunity_id, user_uuid, role)
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
                .where(Opportunity.id == current.id)
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

    return {"id": str(current.id), "placement_type": row[1]}


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
        # Resolves supersede chains, so the write lands on the current revision.
        current = await load_editable_opportunity(session, opportunity_id, user_uuid, role)
        updated = (
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == current.id)
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
        "id": str(current.id),
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

        candidate = await load_visible_candidate(session, candidate_id, user_uuid, role)

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
    buddy_name: str | None = None,
    documents: list[dict] | None = None,
    revision_of_opportunity_id: uuid.UUID | None = None,
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
        # The buddy who referred this client — the external recruiter who owns
        # the account. Shown as the "Owner" in the job order panel for a
        # single-user workspace where the internal assignee is always the one
        # signed-in user and carries no information.
        "buddy_name": buddy_name,
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
        # The revision chain, both directions. `superseded_by_opportunity_id`
        # says this row was replaced by a later one (NULL = current); the list
        # only ever shows current rows, but the field stays on the payload so a
        # row fetched by id answers which revision it is. `revision_of` is the
        # reverse: this row replaced an earlier one, which lets the UI say
        # "requirements updated" rather than showing the new row as if it were
        # a fresh vacancy.
        "superseded_by_opportunity_id": (
            str(row.superseded_by_opportunity_id)
            if row.superseded_by_opportunity_id
            else None
        ),
        "superseded_at": row.superseded_at.isoformat() if row.superseded_at else None,
        "revision_of_opportunity_id": (
            str(revision_of_opportunity_id) if revision_of_opportunity_id else None
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
        # The two human decisions on the row, returned wherever the row is.
        # The panel's placement form reads its initial values straight off
        # whatever row it was handed, so a payload without them draws "not
        # set" over a placement type somebody set — and `placement_type` is
        # what unlocks the lawful sex filter, so drawing it wrong is not a
        # cosmetic loss. `_set_by`/`_set_at` stay out: they are the audit
        # trail, not something the form renders.
        "placement_type": row.placement_type,
        "sex_requirement": row.sex_requirement,
        "sex_requirement_reason": row.sex_requirement_reason,
        # Translated, not passed through: the chips, the `status` parameter and
        # this field have to be the same vocabulary or a client cannot filter
        # on what it just rendered. `ready` becomes `new`; anything the
        # pipeline invents later passes through unrenamed rather than being
        # forced into a bucket it does not belong in.
        "review_status": _STORED_TO_FILTER.get(row.review_status, row.review_status),
        # The job-description files this vacancy came with. Always an array —
        # empty for the ordinary row with no attachment — so the browser never
        # has to treat "absent" differently from "none".
        "documents": documents or [],
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

    `document_id` is the optional id of a job-description file uploaded in the
    same dialog and read by the worker; when present, the new vacancy is linked
    to it so the file travels with the row.
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
    document_id: uuid.UUID | None = None


@router.post("/opportunities/{opportunity_id}/claim")
async def claim_opportunity(opportunity_id: uuid.UUID, request: Request) -> dict:
    """Take an unassigned job order.

    The `WHERE assigned_user_id IS NULL` is the whole concurrency story: two
    recruiters pressing this at the same moment both run the same UPDATE, and
    exactly one of them matches a row. A SELECT to check first and an UPDATE
    to act would reintroduce precisely the gap this clause closes — the second
    reader would see NULL too, and both would be told they had won.

    It runs BEFORE the visibility read, which is the opposite of every other
    route here and is explained where it happens: the loser of a race has to
    be told 409, and by the time they ask, the row they lost is invisible to
    them.

    Deliberately not behind `load_editable_opportunity`, and
    `tests/test_opportunity_routes_guarded.py` exempts it for that reason:
    `can_edit` refuses an unassigned job order on purpose, and an unassigned
    job order is the only kind this route acts on. Claiming is the act that
    makes it editable.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        # Resolve the supersede chain first — a claim must land on the current
        # revision, never on a row a later email already replaced. This read is
        # deliberately NOT `load_visible_opportunity`: it applies no visibility
        # predicate, and that is safe because it only follows ids — RLS still
        # confines every row to this tenant, and the compare-and-set below can
        # only match an unassigned row, which is visible to the whole agency
        # anyway. Applying visibility here would reintroduce the exact race the
        # route exists to avoid: the loser's row is assigned, hence invisible,
        # and they would get a 404 instead of the honest 409.
        claim_target = await _current_revision_id(session, opportunity_id)

        # The compare-and-set goes FIRST, before any visibility read, and that
        # is safe rather than a shortcut: it can only match a row that is in
        # this tenant (RLS) and has `assigned_user_id IS NULL`, and an
        # unassigned row is the first disjunct of `visible_opportunities` —
        # visible to every member of the agency. So there is no row this
        # statement can touch that the read guard would have refused.
        #
        # Doing it the other way round is what made the loser of a race lie.
        # Once the winner commits, the row is assigned to them: for the loser
        # it is no longer unassigned, not theirs, and not shared, so the
        # visibility read raised 404 "No such job order." about a job order
        # that plainly exists and that they were entitled to try for.
        claimed = (
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == claim_target)
                .where(Opportunity.assigned_user_id.is_(None))
                # A superseded row is historical — its successor is the live job
                # order, and `_current_revision_id` already resolved to it. If a
                # supersede link lands between the pre-read and this CAS, the
                # claim must not land on a row that is about to be hidden.
                .where(Opportunity.superseded_by_opportunity_id.is_(None))
                .values(assigned_user_id=user_uuid)
                .returning(Opportunity.id)
            )
        ).scalar_one_or_none()

        if claimed is None:
            # Nothing matched, which is two different answers. Either the row
            # is not there at all — wrong id, or another agency's, which RLS
            # has already hidden — or it is there and somebody holds it.
            #
            # `load_visible_opportunity` cannot tell those apart here, because
            # the row we are asking about is by definition now assigned, and
            # that is precisely what makes it invisible. So the question asked
            # is the one this route actually needs: does an ASSIGNED row with
            # this id exist inside my agency? Deliberately narrower than
            # dropping the guard: an unassigned row is unreachable by this
            # branch, RLS still confines it to the tenant, and the only thing
            # a colleague learns is that a job order id they already held is
            # taken — which is what they came here to find out.
            taken = (
                await session.execute(
                    select(Opportunity.id)
                    .where(Opportunity.id == claim_target)
                    .where(Opportunity.assigned_user_id.is_not(None))
                )
            ).scalar_one_or_none()
            if taken is None:
                # Not assigned, yet the compare-and-set still missed it: the
                # id names nothing we may touch. `load_visible_opportunity` is
                # the one author of that 404. In the remaining sliver — the
                # row was released again between the two statements — it comes
                # back with a row, and 409 is the closer of the two answers:
                # something happened to it, try again.
                await load_visible_opportunity(
                    session, claim_target, user_uuid, role
                )
            raise HTTPException(
                status_code=409, detail="Someone else has taken this job order."
            )

    # Nothing is emitted. You pressed the button; being told what you just did
    # is noise, and the colleagues who lost the race learn it from their 409.
    return {"id": str(claim_target), "assigned_user_id": str(user_uuid)}


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
        # Resolves supersede chains, so the assignment lands on the current
        # revision — reassigning a stale id must move the live job order.
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
            .where(Opportunity.id == opportunity.id)
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


_CLIENT_NOT_IN_AGENCY = "That client is not in this agency."


async def _load_client_in_agency(session: Any, client_id: uuid.UUID) -> Any:
    """The client row, or `None` when this agency cannot see it.

    RLS scopes the SELECT to the agency, so another agency's client is simply
    not found and reads as the same refusal — which is also the only answer
    that does not confirm the id exists somewhere.
    """
    return (
        await session.execute(
            select(Client.id, Client.name, Client.assigned_user_id).where(
                Client.id == client_id
            )
        )
    ).one_or_none()


def _client_fk_constraint_name() -> str:
    """The name Postgres will use when the client link is refused.

    Read off the mapped table, not written out, because two spellings of one
    name are one rename away from disagreeing — and the disagreement would be
    silent, turning a helpful 422 back into a 500 with every test still green.
    """
    for constraint in Opportunity.__table__.constraints:
        if isinstance(constraint, ForeignKeyConstraint) and "client_id" in {
            element.parent.name for element in constraint.elements
        }:
            name = constraint.name
            assert isinstance(name, str)
            return name
    raise RuntimeError("opportunities has no client foreign key")


_CLIENT_FK_CONSTRAINT = _client_fk_constraint_name()


def _constraint_of(exc: IntegrityError) -> str | None:
    """Which constraint refused the write, as asyncpg reported it.

    asyncpg raises a `PostgresError` carrying `constraint_name`; SQLAlchemy
    wraps it twice over, so the chain is walked rather than any one layer
    trusted. `None` when nothing in the chain says — a driver that does not
    report the name means the failure cannot be attributed, and an
    unattributable failure is left to surface as itself.
    """
    error: BaseException | None = exc.orig
    while error is not None:
        name = getattr(error, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
        error = error.__cause__
    return None


@asynccontextmanager
async def _client_link_conflict_becomes_422() -> AsyncIterator[None]:
    """The half of the guard a pre-check cannot cover.

    A check and the write that follows it are two statements, and no amount of
    checking closes the window where the client is deleted in between. The
    outcome the recruiter needs to hear is identical either way — the client
    they picked is not there — so the composite foreign key's own refusal is
    converted into the same 422 rather than escaping as a 500.

    Only that one constraint. A flush carries every constraint on the row, and
    the same INSERT can fail over the assignee foreign key or a check — the
    recruiter offboarded mid-request, a salary period out of vocabulary — with
    no client involved at all. Reporting those as a missing client is worse
    than a 500: a 500 says something unexpected happened, while this asserts a
    specific falsehood about their data. So the failure is inspected and
    converted only when it names the opportunities→clients foreign key, whose
    name is read off the model rather than spelled out here, so a rename in
    `app/models/opportunity.py` cannot leave this silently matching nothing.

    Callers still enter it only when a `client_id` was actually supplied,
    which is cheap and keeps the intent local. The link route enters it on the
    unlink path too, where `client_id` is None and the foreign key cannot fire.
    """
    try:
        yield
    except IntegrityError as exc:
        if _constraint_of(exc) != _CLIENT_FK_CONSTRAINT:
            raise
        raise HTTPException(
            status_code=422, detail=_CLIENT_NOT_IN_AGENCY
        ) from exc


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
        # Resolves supersede chains: linking a stale id must link the live
        # job order.
        opportunity = await load_visible_opportunity(
            session, opportunity_id, user_uuid, role
        )
        opportunity_id = opportunity.id
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
            client = await _load_client_in_agency(session, body.client_id)
            if client is None:
                raise HTTPException(
                    status_code=422, detail=_CLIENT_NOT_IN_AGENCY
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

        # Which client a job order came from is a fact, not a claim on it, so
        # it is written unconditionally and on its own. The assignee is *not*
        # written here: `adopted` came from a row read moments ago, and writing
        # it back unconditionally would silently overwrite a claim that landed
        # in between — including when nothing was adopted at all, where the
        # stale NULL would undo the winner's claim.
        async with _client_link_conflict_becomes_422():
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == opportunity_id)
                .values(client_id=body.client_id)
            )

        if adopted != previous_assignee:
            # Compare-and-set, exactly as `claim_opportunity` does: adoption is
            # only ever from unassigned, so `WHERE assigned_user_id IS NULL` is
            # both the precondition and the race guard. No match means someone
            # claimed it first, and they keep it.
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == opportunity_id)
                .where(Opportunity.assigned_user_id.is_(None))
                .values(assigned_user_id=adopted)
            )

        # Whatever the row actually says now, which is not necessarily what
        # this request hoped for. Re-read rather than assume, so the response
        # never tells the browser about an assignee it does not have.
        assignee = (
            await session.execute(
                select(Opportunity.assigned_user_id).where(
                    Opportunity.id == opportunity_id
                )
            )
        ).scalar_one()

        name = None
        if assignee is not None:
            name = (
                await session.execute(
                    select(_assignee_name_expr(User)).where(User.id == assignee)
                )
            ).scalar_one_or_none()

    # A job order quietly becoming yours is exactly what the assigned event
    # exists to announce. `assign_opportunity` emits it; this route assigns
    # too, so it must as well, or adoption is the one way to gain work without
    # being told. Emitted after the transaction commits, matching the ordering
    # `opportunity_shares.py` uses and explains.
    # `assignee`, not `adopted`: if the compare-and-set lost, telling the
    # would-be adopter they were given a job order they do not have is worse
    # than saying nothing, and the winner claimed it themselves and needs no
    # announcement.
    if assignee is not None and assignee == adopted and adopted != previous_assignee:
        await emit_and_enqueue(
            OpportunityEvent(
                kind=EVENT_OPPORTUNITY_ASSIGNED,
                tenant_id=tenant_uuid,
                opportunity_id=opportunity_id,
                recipient_user_ids=(assignee,),
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
        "assigned_user_id": str(assignee) if assignee else None,
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
    resolved_client_id = body.client_id
    async with tenant_session(tenant_uuid) as session:
        # Refused here, before the insert, for the reason the link route gives:
        # a client picked from the list and deleted or merged before Save is
        # pressed would otherwise reach the composite foreign key and come back
        # as a 500. Same sentence as that route, because it is the same fact.
        if body.client_id is not None:
            if await _load_client_in_agency(session, body.client_id) is None:
                raise HTTPException(
                    status_code=422, detail=_CLIENT_NOT_IN_AGENCY
                )
        elif body.company_name_raw is not None and body.company_name_raw.strip():
            # No client was picked, but a company name was typed: resolve it
            # to a client — matching an existing one on its normalised name,
            # otherwise creating one — in the same transaction as the
            # opportunity insert below, so a failed request cannot leave an
            # orphan client behind. `body.client_id is not None` above always
            # wins over this; a recruiter who picked a client is never
            # second-guessed by what they also typed.
            resolved_client_id = await resolve_or_create_client_by_name(
                session, tenant_uuid, body.company_name_raw
            )
        # A job-description file uploaded in this same dialog must belong to
        # this tenant AND not already be linked to a vacancy, or linking it
        # below would be a cross-tenant write or a silent re-bind. Document ids
        # travel in every opportunity payload, so a tenant member could name a
        # colleague's already-linked file — re-attaching it to their own row and
        # silently detaching it from the original. 422 rather than 404: the
        # document was named by the request, and the sentence tells the
        # recruiter what is wrong with the file rather than pretending their
        # vacancy is not theirs.
        if body.document_id is not None:
            document = await session.get(
                OpportunityDocument, body.document_id
            )
            if document is None or document.opportunity_id is not None:
                raise HTTPException(
                    status_code=422,
                    detail="The attached file is no longer available. Remove it and try again.",
                )
        session.add(
            Opportunity(
                id=opportunity_id,
                tenant_id=tenant_uuid,
                email_message_id=None,
                # When it was taken down, which is the closest honest answer to
                # "when did this arrive" and keeps it sorting beside the
                # extracted rows instead of sinking under every NULL.
                received_datetime=datetime.now(UTC),
                client_id=resolved_client_id,
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
        # Flushed inside the guard rather than left to the commit at the end of
        # the block, so a client deleted between the check above and this write
        # is answered with the same 422 instead of escaping as a 500 from a
        # commit nobody is watching. Covers the resolved-by-name path too: it
        # inserts within this same transaction, so nothing has had a chance to
        # delete it before the flush, but the guard costs nothing to keep and
        # keeps the two paths identical below this point.
        if resolved_client_id is not None:
            async with _client_link_conflict_becomes_422():
                await session.flush()
        else:
            # Nothing here was written *because* of a client, so nothing here
            # may be blamed on one. A violation of any other constraint —
            # or of the assignee foreign key, if this recruiter's account was
            # offboarded between sign-in and now — escapes as a 500, which
            # says something unexpected happened rather than asserting a
            # falsehood about data the request never sent.
            await session.flush()

        # Link the uploaded job-description file now that the vacancy exists.
        # The document row was validated above as belonging to this tenant;
        # RLS confines the update to it regardless. Its object key already
        # carries the provisional opportunity id minted at upload, so a
        # re-upload after deleting this vacancy would compute a fresh key —
        # the old bytes are removed by the document's delete route.
        if body.document_id is not None:
            document = await session.get(
                OpportunityDocument, body.document_id
            )
            if document is not None:
                document.opportunity_id = opportunity_id
                await session.flush()

    # No notification: it is already assigned to whoever created it, and they
    # are the only person it concerns.
    return {
        "id": str(opportunity_id),
        "source": Opportunity.MANUAL,
        "assigned_user_id": str(user_uuid),
        "client_id": str(resolved_client_id) if resolved_client_id else None,
        "company_name_raw": body.company_name_raw,
        "job_title_raw": body.job_title_raw,
    }
