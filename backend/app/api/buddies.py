"""Buddy network and user email aliases.

A buddy is an external recruiter who forwards job orders into the user's
mailbox. Email aliases are the user's own alternative addresses — the
forwarding parser must recognise them as "the user", not "a buddy".
"""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, or_, select, text, update

from app.api.auth import _require_session
from app.db.rls import tenant_session
from app.models import Buddy, BuddyReferral, UserEmail
from app.services.name_index import initial_of as _initial_of
from app.services.name_index import sorted_initials as _sorted_initials

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
        else:  # "referral_count" — the aggregate, not a column
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
) -> dict:
    """External recruiters who have referred clients, with referral counts.

    Supports the same letter bar, search and sort as the clients list. Unlike
    clients there is no pagination: a tenant's buddy network is small (the
    partner-agency colleagues who forward job orders), so the whole filtered
    set is returned in one page rather than carving it with limit/offset.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    # The referral count is the aggregate this query is built around, computed
    # once here and reused by the SELECT, the ORDER BY and the count. Naming it
    # keeps the three references reading the same thing.
    referral_count = func.count(BuddyReferral.id)

    async with tenant_session(tenant_uuid) as session:
        base = (
            select(Buddy, referral_count.label("referral_count"))
            .outerjoin(BuddyReferral, BuddyReferral.buddy_id == Buddy.id)
            .group_by(Buddy.id)
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
