"""Buddy network and user email aliases.

A buddy is an external recruiter who forwards job orders into the user's
mailbox. Email aliases are the user's own alternative addresses — the
forwarding parser must recognise them as "the user", not "a buddy".
"""

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select, text

from app.api.auth import _require_session
from app.db.rls import tenant_session
from app.models import Buddy, BuddyReferral, UserEmail

router = APIRouter(tags=["buddies"])


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

@router.get("/buddies")
async def list_buddies(request: Request) -> list[dict]:
    """External recruiters who have referred clients, with referral counts."""
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        rows = (
            await session.execute(
                select(
                    Buddy,
                    func.count(BuddyReferral.id).label("referral_count"),
                )
                .outerjoin(BuddyReferral, BuddyReferral.buddy_id == Buddy.id)
                .group_by(Buddy.id)
                .order_by(func.count(BuddyReferral.id).desc(), Buddy.name)
            )
        ).all()

    return [
        {
            "id": str(row.Buddy.id),
            "name": row.Buddy.name,
            "email": row.Buddy.email,
            "email_domain": row.Buddy.email_domain,
            "source": row.Buddy.source,
            "referral_count": row.referral_count,
        }
        for row in rows
    ]


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
        "source": buddy.source,
        "referrals": [
            {"client_id": str(r.client_id)}
            for r in referrals
        ],
    }
