"""Showing a job order to a colleague.

A share grants sight and nothing else. The recipient may read the job order
and may pass it on to another named colleague — that is how work finds the
right person through a chain — but may not edit it, and may not throw it open
to the whole agency, because that is not theirs to decide about someone
else's client.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.auth import _require_session_with_role
from app.db.rls import tenant_session
from app.models.opportunity_share import OpportunityShare
from app.services.notify.dispatch import emit_and_enqueue
from app.services.notify.events import EVENT_OPPORTUNITY_SHARED, OpportunityEvent
from app.services.visibility import OWNER_ROLE, can_edit, load_visible_opportunity

router = APIRouter(tags=["opportunity-shares"])

# Literal SQL, not `OpportunityShare.scope == ...`, and this is the whole
# subtlety of the module. The uniqueness rules are two PARTIAL indexes, so
# ON CONFLICT has to be given the same predicate to infer which one it means.
# Passed as a column comparison, SQLAlchemy renders the value as a bind
# parameter — `WHERE scope = $8` — and Postgres refuses to match a
# parameterised predicate against an index predicate it must compare at plan
# time: "there is no unique or exclusion constraint matching the ON CONFLICT
# specification". The literal is what makes the two texts equal. These mirror
# `postgresql_where` on the indexes in `app/models/opportunity_share.py`.
_WHERE_USER_SCOPE = text("scope = 'user'")
_WHERE_TENANT_SCOPE = text("scope = 'tenant'")


class ShareRequest(BaseModel):
    scope: str = Field(pattern="^(user|tenant)$")
    user_ids: list[uuid.UUID] = Field(default_factory=list)
    note: str | None = None


@router.post("/opportunities/{opportunity_id}/shares", status_code=201)
async def share_opportunity(
    opportunity_id: uuid.UUID, body: ShareRequest, request: Request, response: Response
) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        # Seeing it is the right to pass it on. 404 if not.
        opportunity = await load_visible_opportunity(
            session, opportunity_id, user_uuid, role
        )
        # Read off the row while the session is open: the values are needed
        # after it closes, and a committed instance's attributes are expired.
        subject = (
            opportunity.job_title_raw,
            opportunity.company_name_raw,
            opportunity.location_raw,
            opportunity.salary_raw,
        )

        if body.scope == OpportunityShare.SCOPE_TENANT:
            # A recipient throwing someone else's client work open to the
            # whole office is not their decision.
            if not can_edit(opportunity, user_uuid, role) and role != OWNER_ROLE:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Only the assigned recruiter can share this with the "
                        "whole agency."
                    ),
                )
            statement = (
                pg_insert(OpportunityShare)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_uuid,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_TENANT,
                    shared_with_user_id=None,
                    shared_by_user_id=user_uuid,
                    note=body.note,
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "opportunity_id"],
                    index_where=_WHERE_TENANT_SCOPE,
                    set_={"note": body.note, "shared_by_user_id": user_uuid},
                )
            )
            await session.execute(statement)
            # None, not an empty tuple. `None` is the "everybody" meaning the
            # event carries; `()` would match no destination at all and the
            # broadcast would notify nobody.
            recipients: tuple[uuid.UUID, ...] | None = None
            shared_count = 0
        else:
            if not body.user_ids:
                raise HTTPException(
                    status_code=422, detail="Name at least one colleague to share with."
                )
            # Only genuinely new recipients are notified. Re-sharing to
            # someone who already has it updates the note silently — they were
            # told the first time, and a second message says nothing new.
            existing = set(
                (
                    await session.execute(
                        select(OpportunityShare.shared_with_user_id).where(
                            OpportunityShare.opportunity_id == opportunity_id,
                            OpportunityShare.scope == OpportunityShare.SCOPE_USER,
                        )
                    )
                ).scalars()
            )
            fresh: list[uuid.UUID] = []
            for target in body.user_ids:
                if target == user_uuid:
                    continue  # sharing with yourself is a no-op, not an error
                if target not in existing:
                    fresh.append(target)
                await session.execute(
                    pg_insert(OpportunityShare)
                    .values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_uuid,
                        opportunity_id=opportunity_id,
                        scope=OpportunityShare.SCOPE_USER,
                        shared_with_user_id=target,
                        shared_by_user_id=user_uuid,
                        note=body.note,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            "tenant_id",
                            "opportunity_id",
                            "shared_with_user_id",
                        ],
                        index_where=_WHERE_USER_SCOPE,
                        set_={"note": body.note, "shared_by_user_id": user_uuid},
                    )
                )
            recipients = tuple(fresh)
            shared_count = len(fresh)
            # Naming colleagues adds rows to an existing conversation rather
            # than creating the one canonical broadcast, and re-sharing is an
            # update — so 200, and 201 is reserved for the broadcast.
            response.status_code = 200

    # Nothing to announce when every named target already had it.
    if recipients is None or recipients:
        job_title, company_name, location, salary = subject
        await emit_and_enqueue(
            OpportunityEvent(
                kind=EVENT_OPPORTUNITY_SHARED,
                tenant_id=tenant_uuid,
                opportunity_id=opportunity_id,
                # None for a tenant broadcast — everybody; a tuple of the newly
                # added recipients for a named share.
                recipient_user_ids=recipients,
                job_title=job_title,
                company_name=company_name,
                location=location,
                salary=salary,
                note=body.note,
            )
        )

    return {"opportunity_id": str(opportunity_id), "newly_shared_with": shared_count}


@router.get("/opportunities/{opportunity_id}/shares")
async def list_shares(opportunity_id: uuid.UUID, request: Request) -> dict:
    """Who can currently see this job order, and who showed it to them."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        rows = (
            (
                await session.execute(
                    select(OpportunityShare)
                    .where(OpportunityShare.opportunity_id == opportunity_id)
                    .order_by(OpportunityShare.created_at)
                )
            )
            .scalars()
            .all()
        )
        items = [
            {
                "id": str(row.id),
                "scope": row.scope,
                "shared_with_user_id": (
                    str(row.shared_with_user_id) if row.shared_with_user_id else None
                ),
                "shared_by_user_id": (
                    str(row.shared_by_user_id) if row.shared_by_user_id else None
                ),
                "note": row.note,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    return {"items": items}


@router.delete("/opportunities/{opportunity_id}/shares/{share_id}", status_code=204)
async def unshare_opportunity(
    opportunity_id: uuid.UUID, share_id: uuid.UUID, request: Request
) -> None:
    """Revokes sight, not history.

    Only the share row goes. Notes, submissions and evidence the recipient
    created while they could see the job order are their work and stay.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        opportunity = await load_visible_opportunity(
            session, opportunity_id, user_uuid, role
        )
        share = (
            await session.execute(
                select(OpportunityShare)
                .where(OpportunityShare.id == share_id)
                .where(OpportunityShare.opportunity_id == opportunity_id)
            )
        ).scalar_one_or_none()
        if share is None:
            raise HTTPException(status_code=404, detail="No such share.")

        allowed = (
            can_edit(opportunity, user_uuid, role)  # the assignee, or the owner
            or share.shared_by_user_id == user_uuid  # whoever passed it on
            or share.shared_with_user_id == user_uuid  # or the recipient, leaving
        )
        if not allowed:
            raise HTTPException(
                status_code=403, detail="This share is not yours to withdraw."
            )

        await session.execute(
            delete(OpportunityShare).where(OpportunityShare.id == share_id)
        )
