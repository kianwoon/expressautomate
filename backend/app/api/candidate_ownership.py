"""Claiming a candidate out of the queue, and handing one over.

Separate from `candidates.py` only because that file is at the repo's
1500-line ceiling. If it is ever split properly, these belong beside it.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, update

from app.api.auth import _require_session_with_role
from app.db.rls import tenant_session
from app.models.candidate import Candidate
from app.models.tenant import User
from app.services.notify.candidate_events import CandidateEvent
from app.services.notify.dispatch import emit_candidate_event
from app.services.notify.events import CANDIDATE_ASSIGNED, CANDIDATE_UNCLAIMED
from app.services.user_naming import actor_name
from app.services.visibility import can_edit_candidate, load_visible_candidate

router = APIRouter(tags=["candidate-ownership"])


class AssignBody(BaseModel):
    """`None` releases the candidate back to the unclaimed queue.

    Explicitly nullable rather than a second endpoint: handing over and
    letting go are the same act with a different destination, and splitting
    them would let the two drift on who gets told.
    """

    user_id: uuid.UUID | None


@router.post("/candidates/{candidate_id}/claim")
async def claim_candidate(candidate_id: uuid.UUID, request: Request) -> dict[str, str]:
    """An atomic UPDATE, not a read-then-write.

    Two recruiters claiming the same candidate at the same moment is a real
    race, not a theoretical one. `WHERE owner_id IS NULL` resolves it in the
    database; the loser gets 409 rather than silently overwriting the winner.

    It cannot go through `load_editable_candidate`: an unowned candidate is
    exactly what `can_edit_candidate` refuses, because claiming is the act
    that creates edit rights. `tests/test_candidate_routes_guarded.py` records
    that exemption.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        result = await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .where(Candidate.owner_id.is_(None))
            .values(owner_id=user_uuid, updated_by=user_uuid)
        )
        if result.rowcount == 0:
            # The UPDATE runs *before* the visibility guard, deliberately.
            # Losing the race makes the candidate somebody else's and so
            # invisible, and asking the guard first would answer the loser
            # with 404 — "there is no such person" — when what happened is
            # that a colleague got there half a second earlier.
            #
            # Nothing is leaked by the 409: an unowned candidate is queue
            # work every recruiter in the agency could already see, so a row
            # present here was on this recruiter's own screen an instant ago.
            exists = await session.scalar(
                select(Candidate.id).where(Candidate.id == candidate_id)
            )
            if exists is None:
                # Not in this agency's book. Raised through the ordinary
                # guard so the status and the wording match every other
                # by-id candidate route.
                await load_visible_candidate(session, candidate_id, user_uuid, role)
            raise HTTPException(status_code=409, detail="A colleague claimed this first.")

    # Nothing is emitted: you did it, you know.
    return {"status": "claimed"}


@router.post("/candidates/{candidate_id}/assign")
async def assign_candidate(
    candidate_id: uuid.UUID, body: AssignBody, request: Request
) -> dict[str, str]:
    """Hand a candidate to a colleague, or release it to the queue."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        candidate = await load_visible_candidate(session, candidate_id, user_uuid, role)
        if not can_edit_candidate(candidate, user_uuid, role):
            raise HTTPException(status_code=403, detail="This candidate is not yours to assign.")
        if body.user_id is not None:
            # RLS scopes this to the agency, so a colleague of another agency
            # is simply absent. Checked rather than left to the foreign key:
            # an unknown id would otherwise surface as a 500.
            target = (
                await session.execute(select(User).where(User.id == body.user_id))
            ).scalar_one_or_none()
            if target is None:
                raise HTTPException(status_code=404, detail="No such colleague.")
        name = candidate.full_name
        me = (
            await session.execute(select(User).where(User.id == user_uuid))
        ).scalar_one_or_none()
        # Denormalised here because the delivery worker rebuilds the event
        # from the outbox row and the candidate row alone — it cannot
        # recover who acted.
        who = None if me is None else actor_name(me.preferred_name, me.display_name, me.email)
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(owner_id=body.user_id, updated_by=user_uuid)
        )

    # Emitted AFTER the session closes, as `opportunity_shares.py` does: a
    # rolled-back transaction must not leave a notification claiming
    # something that did not happen.
    #
    # A released candidate is queue work again and nobody would otherwise
    # learn it is available, so releasing tells the agency (`recipient_user_
    # ids=None` is the tenant-wide meaning); handing over tells one person.
    await emit_candidate_event(
        CandidateEvent(
            kind=CANDIDATE_UNCLAIMED if body.user_id is None else CANDIDATE_ASSIGNED,
            tenant_id=tenant_uuid,
            candidate_id=candidate_id,
            candidate_name=name,
            recipient_user_ids=None if body.user_id is None else (body.user_id,),
            actor_name=who,
        )
    )
    return {"status": "assigned"}
