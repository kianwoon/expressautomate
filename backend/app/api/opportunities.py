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

Two later decisions shape the payload. The chip counts are tenant-wide and
computed apart from the page, because a count that shrank as you paged would
be answering a different question than the one it appears to answer. And the
trust signal is `verified_fields / total_fields` — how many extracted values
were found verbatim in the source — not a model confidence score.
`model_confidence` exists for calibration and is never rendered as a
probability (see `models/extraction.py`); it does not appear in any response
here, and a test asserts that it never starts to.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import aliased

from app.api.auth import _require_session
from app.core.config import settings
from app.db.rls import tenant_session
from app.models import EmailMessage, Opportunity, OpportunityCode
from app.models.extraction import ExtractionEvidence

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


class ReviewRequest(BaseModel):
    """Explicitly two-way: the same endpoint un-reviews.

    A one-way "mark reviewed" leaves a mis-click permanent, and the only escape
    a recruiter would have is to stop trusting the chip counts.
    """

    reviewed: bool


@router.get("/opportunities")
async def list_opportunities(
    request: Request,
    # Resolved in the body rather than declared as `= settings.…`: a default in
    # the signature is bound once at import, so the setting would freeze at the
    # value it had when the module loaded and no later change could move it.
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    status: StatusFilter | None = None,
) -> dict:
    """The signed-in user's agency's vacancies, newest first.

    Deliberately *not* gated on the mailbox scope, unlike the endpoints in
    `mailbox.py`: a revoked grant stops new mail arriving, it does not make
    the job orders already extracted anyone else's business. Locking the list
    behind a live grant would blank the page for the one user who most needs
    to see what was collected before it broke.
    """
    _user_uuid, tenant_uuid = _require_session(request)

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
        # The chips are counted over the whole tenant, in their own query,
        # before any filter or window is applied. A count that moved with the
        # page would tell the recruiter there are 12 vacancies needing review
        # on page one and 3 on page two, which is not a smaller truth — it is
        # a different question than the one the chip appears to answer.
        counts = {name: 0 for name in _FILTER_TO_STORED}
        counts["all"] = 0
        for stored, n in await session.execute(
            select(Opportunity.review_status, func.count())
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
        base = select(Opportunity, email.internet_message_id, email.graph_message_id).join(
            email, email.id == Opportunity.email_message_id
        )
        if status is not None:
            base = base.where(Opportunity.review_status == _FILTER_TO_STORED[status])

        total = (
            await session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()

        rows = (
            await session.execute(
                base
                # `nulls_last`: an extraction that could not date the email
                # belongs at the bottom of the list, not above this morning's
                # mail. Postgres sorts NULLs first under DESC by default, which
                # is the opposite of what "newest first" means to a reader.
                # `id` breaks ties so paging is stable rather than arbitrary.
                .order_by(
                    Opportunity.received_datetime.desc().nulls_last(),
                    Opportunity.id.desc(),
                )
                .limit(page_limit)
                .offset(offset)
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
    _user_uuid, tenant_uuid = _require_session(request)
    new_status = _REVIEWED if body.reviewed else _READY

    async with tenant_session(tenant_uuid) as session:
        # No ownership check in the WHERE clause beyond the id. That is not an
        # omission: the RLS policy on `opportunities` carries both USING and
        # WITH CHECK, so another agency's row is not visible to this UPDATE and
        # the statement matches nothing. Re-stating `tenant_id = …` here would
        # imply the isolation lives in this line, and the next endpoint written
        # without it would look safe by comparison.
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
