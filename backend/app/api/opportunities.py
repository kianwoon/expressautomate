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
"""

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.auth import _require_session
from app.core.config import settings
from app.db.rls import tenant_session
from app.models import Opportunity

router = APIRouter(tags=["opportunities"])


@router.get("/opportunities")
async def list_opportunities(request: Request) -> dict:
    """The signed-in user's agency's vacancies, newest first.

    Deliberately *not* gated on the mailbox scope, unlike the endpoints in
    `mailbox.py`: a revoked grant stops new mail arriving, it does not make
    the job orders already extracted anyone else's business. Locking the list
    behind a live grant would blank the page for the one user who most needs
    to see what was collected before it broke.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    # Every read goes through `tenant_session`, which sets `app.tenant_id` for
    # the transaction. Without it RLS returns zero rows rather than everyone's
    # — the failure is visible, but it is still a failure, and a plain
    # `SessionLocal()` here would be one edit away from a cross-agency leak.
    async with tenant_session(tenant_uuid) as session:
        rows = (
            await session.execute(
                select(Opportunity)
                # `nulls_last`: an extraction that could not date the email
                # belongs at the bottom of the list, not above this morning's
                # mail. Postgres sorts NULLs first under DESC by default, which
                # is the opposite of what "newest first" means to a reader.
                # `id` breaks ties so paging is stable rather than arbitrary.
                .order_by(
                    Opportunity.received_datetime.desc().nulls_last(),
                    Opportunity.id.desc(),
                )
                # Bounded because a year of ingestion is tens of thousands of
                # rows and the page renders all of them. The number is a
                # setting: what a browser can hold is an operational fact, not
                # a property of this code.
                .limit(settings.OPPORTUNITIES_PAGE_LIMIT)
            )
        ).scalars().all()

    return {
        "limit": settings.OPPORTUNITIES_PAGE_LIMIT,
        "opportunities": [_payload(row) for row in rows],
    }


def _payload(row: Opportunity) -> dict:
    """One row, with absences preserved as absences."""
    return {
        "id": str(row.id),
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
        "review_status": row.review_status,
    }
