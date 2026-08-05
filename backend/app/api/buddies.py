"""Buddy network and user email aliases.

A buddy is an external recruiter who forwards job orders into the user's
mailbox. Email aliases are the user's own alternative addresses — the
forwarding parser must recognise them as "the user", not "a buddy".
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import and_, func, or_, select, text, update

from app.api.auth import _require_session, _require_session_with_role
from app.db.rls import tenant_session
from app.models import Buddy, BuddyReferral, Opportunity, UserEmail
from app.services.name_index import initial_of as _initial_of
from app.services.name_index import sorted_initials as _sorted_initials
from app.services.visibility import visible_opportunities

router = APIRouter(tags=["buddies"])

# A single letter or `#`; anything else is a 422 from the framework rather than
# a hand-rolled check, so the contract lives in the OpenAPI schema too. Shared
# shape with `clients.py` / `candidates.py`, kept here as a local singleton for
# the same B008 reason documented there.
InitialFilter = Query(default=None, pattern=r"^([A-Za-z]|#)$")

# The columns a recruiter may sort by. Anything else is a 422 from the
# framework, so the whitelist is the type system rather than a lookup that
# could silently ignore a typo. These are the four columns the table shows
# headers for; Mobile (phone) is not worth sorting on.
BuddySortBy = Literal["name", "email", "email_domain", "referral_count"]

# A period the referral count and the referral list can be scoped to. The
# recruiter's question is "who has been sending me work *lately*", not "who has
# ever sent me the most" — a buddy who referred ten clients two years ago and
# nothing since is not who this week's review is about. `None` is all time.
BuddyPeriod = Literal["7d", "14d", "30d"]

_PERIOD_DAYS: dict[BuddyPeriod, int] = {"7d": 7, "14d": 14, "30d": 30}


def _period_cutoff(period: BuddyPeriod | None) -> datetime | None:
    """The timestamp a referral's job order must land at or after to count.

    `received_datetime` is the email's arrival — the moment the work actually
    arrived in the mailbox — rather than the buddy→client link's `created_at`,
    which is set once and never updated. A buddy who first referred Acme three
    years ago and forwards a new Acme job order today should count *today*.
    """
    if period is None:
        return None
    return datetime.now(UTC) - timedelta(days=_PERIOD_DAYS[period])


# ---------------------------------------------------------------------------
# User email aliases
# ---------------------------------------------------------------------------

class UserEmailIn(BaseModel):
    email: EmailStr


@router.get("/user/emails")
async def list_user_emails(request: Request) -> list[dict]:
    """The signed-in user's declared email aliases."""
    user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        rows = (
            await session.execute(
                select(UserEmail)
                .where(UserEmail.user_id == user_uuid)
                .order_by(UserEmail.created_at)
            )
        ).scalars().all()

    return [
        {
            "id": str(row.id),
            "email": row.email,
            "verified": row.verified_at is not None,
        }
        for row in rows
    ]


@router.post("/user/emails", status_code=201)
async def add_user_email(request: Request, body: UserEmailIn) -> dict:
    """Declare an additional email address as the user's own."""
    user_uuid, tenant_uuid = _require_session(request)
    email = body.email.lower().strip()

    async with tenant_session(tenant_uuid) as session:
        existing = (
            await session.execute(
                select(UserEmail).where(UserEmail.email == email)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="Email already claimed")

        row = UserEmail(
            id=uuid.uuid4(),
            tenant_id=tenant_uuid,
            user_id=user_uuid,
            email=email,
        )
        session.add(row)
        await session.flush()
        return {"id": str(row.id), "email": row.email, "verified": False}


@router.delete("/user/emails/{email_id}", status_code=204)
async def delete_user_email(request: Request, email_id: uuid.UUID) -> None:
    """Remove an email alias."""
    user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        row = (
            await session.execute(
                select(UserEmail).where(
                    UserEmail.id == email_id, UserEmail.user_id == user_uuid
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Email alias not found")
        await session.execute(
            text("DELETE FROM user_emails WHERE id = :id"), {"id": email_id}
        )


# ---------------------------------------------------------------------------
# Buddies
# ---------------------------------------------------------------------------

def _referral_counts_subquery(cutoff: datetime | None, visible):
    """One buddy → number of job orders they have referred, in a subquery.

    A referral is a buddy→client link, but the number a recruiter cares about
    is *work*, not bookkeeping: "how many job orders has this buddy sent me?".
    So the count is `Opportunity.id` joined through the referral's `client_id`,
    not `BuddyReferral.id`. A buddy who forwards five job orders for the same
    client is five, not one — the same way the opportunities list attributes
    each of those rows' `buddy_name`.

    `visible` is the `visible_opportunities(user_uuid, role)` clause, applied
    here for the same reason `list_opportunities` applies it: RLS scopes to the
    tenant, but visibility within a tenant is per-recruiter (assigned, shared,
    or owned), and a buddy's referred job order the reader cannot see must not
    count any more than it would appear in the job orders list.

    Computed in a subquery and LEFT JOINed so a buddy with no opportunities in
    the period still appears with count 0. The `cutoff` lives in the JOIN
    condition, not a WHERE: a WHERE on a LEFT JOIN's right table would turn it
    back into an inner join and drop the zero-count buddies exactly when the
    period filter is the thing being asked about.
    """
    joined = (
        select(
            BuddyReferral.buddy_id.label("buddy_id"),
            func.count(Opportunity.id).label("n"),
        )
        .select_from(BuddyReferral)
        .join(
            Opportunity,
            and_(
                Opportunity.client_id == BuddyReferral.client_id,
                Opportunity.tenant_id == BuddyReferral.tenant_id,
            ),
        )
        .where(visible)
        .group_by(BuddyReferral.buddy_id)
    )
    if cutoff is not None:
        joined = joined.where(Opportunity.received_datetime >= cutoff)
    return joined.subquery()


def _buddy_order(sort_by, descending, initial, referral_count):
    """The ORDER BY clauses for the buddies list, as a tuple SQLAlchemy spreads.

    Three branches, the same three `clients._client_order` answers:

    * An explicit `sort_by` — the recruiter clicked a column header. Honoured
      exactly, direction included.
    * A letter selected with no explicit sort — "find this buddy" reads as
      alphabetical. Recency or referral count inside a letter is no order at
      all: you cannot scan for a name in it.
    * Nothing — the default view, where referral count wins (the buddies who
      send the most work sit at the top) and name breaks the tie. This is the
      order the list had before sorting existed, preserved so the default does
      not move.

    `Buddy.id` last on every branch for the same reason clients appends
    `Client.id`: a non-unique sort key lets Postgres return ties in a different
    order each time, and paging then shows somebody twice and somebody else not
    at all.
    """
    if sort_by is not None:
        if sort_by == "name":
            key = func.lower(Buddy.name)
            keys = (key, Buddy.email)
        elif sort_by == "email":
            keys = (Buddy.email,)
        elif sort_by == "email_domain":
            keys = (Buddy.email_domain,)
        else:  # "referral_count" — the joined count column, not an aggregate
            keys = (referral_count,)
        ordered = tuple(
            k.desc().nullslast() if descending else k.asc().nullslast() for k in keys
        )
        return (*ordered, Buddy.id.asc())

    if initial is not None:
        return (func.lower(Buddy.name).asc(), Buddy.id.asc())

    return (referral_count.desc(), func.lower(Buddy.name).asc(), Buddy.id.asc())


@router.get("/buddies")
async def list_buddies(
    request: Request,
    q: str | None = None,
    initial: str | None = InitialFilter,
    sort_by: BuddySortBy | None = None,
    descending: bool = False,
    period: BuddyPeriod | None = None,
) -> dict:
    """External recruiters who have referred clients, with referral counts.

    Supports the same letter bar, search and sort as the clients list. Unlike
    clients there is no pagination: a tenant's buddy network is small (the
    partner-agency colleagues who forward job orders), so the whole filtered
    set is returned in one page rather than carving it with limit/offset.

    `referral_count` is the number of job orders a buddy has brought in — the
    work, not the buddy→client links — and `period` scopes it to a window so a
    recruiter can ask "who has been sending me work lately".
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    cutoff = _period_cutoff(period)
    # The same per-recruiter visibility the job orders list applies. RLS scopes
    # to the tenant; this scopes within it, so a buddy's referred job order the
    # reader cannot see does not count any more than it would appear in their
    # job orders list.
    visible = visible_opportunities(user_uuid, role)

    # The referral count, once, as a LEFT JOINed column. Buddies with no
    # opportunities in the period coalesce to 0 rather than disappearing.
    counts = _referral_counts_subquery(cutoff, visible)
    referral_count = func.coalesce(counts.c.n, 0).label("referral_count")

    async with tenant_session(tenant_uuid) as session:
        base = (
            select(Buddy, referral_count)
            .outerjoin(counts, counts.c.buddy_id == Buddy.id)
        )

        if q:
            # Same escaping as `clients.py`: parameterized (no injection), but
            # a literal "%" or "_" typed by a recruiter would otherwise match
            # as a wildcard. Matched against email as well as name so a search
            # for a domain or address finds the buddy the way a recruiter would
            # think of them.
            escaped = (
                q.strip()
                .lower()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            like = f"%{escaped}%"
            base = base.where(
                or_(
                    func.lower(Buddy.name).like(like, escape="\\"),
                    func.lower(Buddy.email).like(like, escape="\\"),
                )
            )

        # Computed from `base` *before* `initial` narrows it, for the same
        # reason as clients/candidates: the bar answers "which letters could I
        # click next", and applying the letter already clicked would leave a
        # bar of one letter with no way back.
        unfiltered = base.subquery()
        initials = _sorted_initials(
            list(
                (
                    await session.execute(
                        select(_initial_of(unfiltered.c.name))
                        .select_from(unfiltered)
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
        )

        if initial is not None:
            base = base.where(_initial_of(Buddy.name) == initial.upper())

        order = _buddy_order(sort_by, descending, initial, referral_count)
        rows = (await session.execute(base.order_by(*order))).all()

    return {
        "items": [
            {
                "id": str(row.Buddy.id),
                "name": row.Buddy.name,
                "email": row.Buddy.email,
                "email_domain": row.Buddy.email_domain,
                "phone": row.Buddy.phone,
                "source": row.Buddy.source,
                "referral_count": row.referral_count,
            }
            for row in rows
        ],
        "total": len(rows),
        "initials": initials,
    }


class BuddyUpdate(BaseModel):
    phone: str | None = None


@router.patch("/buddies/{buddy_id}")
async def update_buddy(request: Request, buddy_id: uuid.UUID, body: BuddyUpdate) -> dict:
    """Update a buddy's editable fields (phone)."""
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        buddy = (
            await session.execute(
                select(Buddy).where(Buddy.id == buddy_id)
            )
        ).scalar_one_or_none()
        if buddy is None:
            raise HTTPException(status_code=404, detail="Buddy not found")

        await session.execute(
            update(Buddy).where(Buddy.id == buddy_id).values(phone=body.phone)
        )

    return {
        "id": str(buddy_id),
        "name": buddy.name,
        "email": buddy.email,
        "email_domain": buddy.email_domain,
        "phone": body.phone,
        "source": buddy.source,
    }


@router.get("/buddies/{buddy_id}")
async def get_buddy(request: Request, buddy_id: uuid.UUID) -> dict:
    """Buddy detail with the clients they have referred."""
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        buddy = (
            await session.execute(
                select(Buddy).where(Buddy.id == buddy_id)
            )
        ).scalar_one_or_none()
        if buddy is None:
            raise HTTPException(status_code=404, detail="Buddy not found")

        referrals = (
            await session.execute(
                select(BuddyReferral).where(BuddyReferral.buddy_id == buddy_id)
            )
        ).scalars().all()

    return {
        "id": str(buddy.id),
        "name": buddy.name,
        "email": buddy.email,
        "email_domain": buddy.email_domain,
        "phone": buddy.phone,
        "source": buddy.source,
        "referrals": [
            {"client_id": str(r.client_id)}
            for r in referrals
        ],
    }


@router.get("/buddies/{buddy_id}/referrals")
async def list_buddy_referrals(
    request: Request,
    buddy_id: uuid.UUID,
    period: BuddyPeriod | None = None,
) -> dict:
    """The job orders a buddy has referred, for the count → modal.

    Resolved through the same chain the count uses — a referral's client → its
    opportunities — so the list and the number on the row agree. `period` is
    the same window the count is scoped to, passed through so opening the modal
    shows exactly the job orders behind the number the recruiter clicked.

    Returns a compact shape rather than the full opportunity payload: the modal
    is a scan-and-recognise list, not a detail view, and the columns a recruiter
    uses to tell one forwarded job order from another are position, company,
    when it arrived, the salary and where it is.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    cutoff = _period_cutoff(period)
    # The same per-recruiter visibility the count and the job orders list
    # apply, so the modal lists exactly the rows behind the number the
    # recruiter clicked — nothing more, nothing less.
    visible = visible_opportunities(user_uuid, role)

    async with tenant_session(tenant_uuid) as session:
        buddy = (
            await session.execute(select(Buddy).where(Buddy.id == buddy_id))
        ).scalar_one_or_none()
        if buddy is None:
            raise HTTPException(status_code=404, detail="Buddy not found")

        stmt = (
            select(Opportunity)
            .join(
                BuddyReferral,
                and_(
                    BuddyReferral.client_id == Opportunity.client_id,
                    BuddyReferral.tenant_id == Opportunity.tenant_id,
                ),
            )
            .where(BuddyReferral.buddy_id == buddy_id)
            .where(visible)
            .order_by(Opportunity.received_datetime.desc().nullslast(), Opportunity.id.desc())
        )
        if cutoff is not None:
            stmt = stmt.where(Opportunity.received_datetime >= cutoff)
        rows = (await session.execute(stmt)).scalars().all()

    return {
        "buddy": {"id": str(buddy.id), "name": buddy.name},
        "items": [
            {
                "id": str(o.id),
                "job_title_raw": o.job_title_raw,
                "company_name_raw": o.company_name_raw,
                "received_datetime": (
                    o.received_datetime.isoformat() if o.received_datetime else None
                ),
                "location_raw": o.location_raw,
                "salary_raw": o.salary_raw,
            }
            for o in rows
        ],
        "total": len(rows),
    }
