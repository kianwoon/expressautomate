"""Showing a candidate to a colleague, and asking to be shown one.

A share grants sight and nothing else. Exactly one person — the owner — can
edit a candidate, so there is no permission lattice here: the recipient may
read the record and may pass it on to another named colleague, because that is
how a person finds the right recruiter through a chain. Throwing somebody
else's candidate open to the whole agency is not theirs to decide.

The other half is the collision. A recruiter who meets a candidate a colleague
already holds sees nothing at all — `load_visible_candidate` 404s — so there
has to be a way to ask. `request_candidate_access` is that way, and it is the
one route in the codebase deliberately reachable for an invisible row.

Structurally a mirror of `opportunity_shares.py`, deliberately: one sharing
idiom in this codebase rather than two.
"""

import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.auth import _require_session_with_role
from app.db.rls import tenant_session
from app.models.candidate import Candidate
from app.models.candidate_access_request import CandidateAccessRequest
from app.models.candidate_share import CandidateShare
from app.services.notify.candidate_events import CandidateEvent
from app.services.notify.dispatch import emit_candidate_event
from app.services.notify.events import (
    CANDIDATE_ACCESS_DECLINED,
    CANDIDATE_ACCESS_GRANTED,
    CANDIDATE_ACCESS_REQUESTED,
    CANDIDATE_SHARED,
)
from app.services.visibility import (
    OWNER_ROLE,
    can_edit_candidate,
    load_visible_candidate,
)

router = APIRouter(tags=["candidate-shares"])

# Literal SQL, not `CandidateShare.scope == ...`, and this is the whole
# subtlety of the module — see the identical note in `opportunity_shares.py`.
# The uniqueness rules are PARTIAL indexes, so ON CONFLICT has to be given the
# same predicate to infer which one it means. Passed as a column comparison,
# SQLAlchemy renders the value as a bind parameter (`WHERE scope = $8`), and
# Postgres refuses to match a parameterised predicate against an index
# predicate it must compare at plan time: "there is no unique or exclusion
# constraint matching the ON CONFLICT specification". The literal is what makes
# the two texts equal. These mirror `postgresql_where` on the indexes in
# `app/models/candidate_share.py` and `app/models/candidate_access_request.py`.
_WHERE_USER_SCOPE = text("scope = 'user'")
_WHERE_TENANT_SCOPE = text("scope = 'tenant'")
_WHERE_PENDING = text("status = 'pending'")


class ShareRequest(BaseModel):
    scope: str = Field(pattern="^(user|tenant)$")
    user_ids: list[uuid.UUID] = Field(default_factory=list)
    note: str | None = None


class AccessRequestBody(BaseModel):
    note: str | None = None


def _may_resolve(candidate: Candidate, user_id: uuid.UUID, role: str) -> bool:
    """Who may answer a request: the candidate's owner, and `role='owner'`.

    Delegates to `can_edit_candidate` rather than restating "owner, or
    role='owner'": restating it is how the two drift. The name is separate
    because the question is different — answering a request is not editing the
    record — so if they ever have to diverge, there is already a place to say
    so.
    """
    return can_edit_candidate(candidate, user_id, role)


# ---------------------------------------------------------------- shares


@router.post("/candidates/{candidate_id}/shares", status_code=201)
async def create_candidate_share(
    candidate_id: uuid.UUID, body: ShareRequest, request: Request
) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        # Seeing it is the right to pass it on. 404 if not.
        candidate = await load_visible_candidate(
            session, candidate_id, user_uuid, role
        )
        # Read off the row while the session is open: the value is needed
        # after it closes, and a committed instance's attributes are expired.
        candidate_name = candidate.full_name

        # A recipient may pass a candidate onward to a named colleague — that
        # is how a person finds the right recruiter through a chain. Throwing
        # somebody else's candidate open to the whole office is not theirs to
        # decide, so the broadcast is restricted to the owner and role='owner'.
        if body.scope == CandidateShare.SCOPE_TENANT and not can_edit_candidate(
            candidate, user_uuid, role
        ):
            raise HTTPException(
                status_code=403,
                detail="Only the owner may share this candidate with everyone.",
            )

        if body.scope == CandidateShare.SCOPE_TENANT:
            await session.execute(
                pg_insert(CandidateShare)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_uuid,
                    candidate_id=candidate_id,
                    scope=CandidateShare.SCOPE_TENANT,
                    shared_with_user_id=None,
                    shared_by_user_id=user_uuid,
                    note=body.note,
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "candidate_id"],
                    index_where=_WHERE_TENANT_SCOPE,
                    set_={"note": body.note, "shared_by_user_id": user_uuid},
                )
            )
            # None, not an empty tuple. `None` is the "everybody" meaning the
            # event carries; `()` would match no destination at all and the
            # broadcast would notify nobody.
            recipients: tuple[uuid.UUID, ...] | None = None
            shared_count = 0
        else:
            if not body.user_ids:
                raise HTTPException(
                    status_code=422,
                    detail="Name at least one colleague to share with.",
                )
            # Only genuinely new recipients are notified. Re-sharing to
            # someone who already has it updates the note silently — they were
            # told the first time, and a second message says nothing new.
            existing = set(
                (
                    await session.execute(
                        select(CandidateShare.shared_with_user_id).where(
                            CandidateShare.candidate_id == candidate_id,
                            CandidateShare.scope == CandidateShare.SCOPE_USER,
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
                    pg_insert(CandidateShare)
                    .values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_uuid,
                        candidate_id=candidate_id,
                        scope=CandidateShare.SCOPE_USER,
                        shared_with_user_id=target,
                        shared_by_user_id=user_uuid,
                        note=body.note,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            "tenant_id",
                            "candidate_id",
                            "shared_with_user_id",
                        ],
                        index_where=_WHERE_USER_SCOPE,
                        set_={"note": body.note, "shared_by_user_id": user_uuid},
                    )
                )
            recipients = tuple(fresh)
            shared_count = len(fresh)

    # Deliberately after commit: the share row must exist before anyone is
    # told about it. Not atomic — if this raises, the share exists with no
    # notification. That is the acceptable failure mode; the alternative risks
    # announcing a share that was never written.
    # Nothing to announce when every named target already had it.
    if recipients is None or recipients:
        await emit_candidate_event(
            CandidateEvent(
                kind=CANDIDATE_SHARED,
                tenant_id=tenant_uuid,
                candidate_id=candidate_id,
                candidate_name=candidate_name,
                recipient_user_ids=recipients,
                note=body.note,
            )
        )

    return {"candidate_id": str(candidate_id), "newly_shared_with": shared_count}


@router.get("/candidates/{candidate_id}/shares")
async def list_candidate_shares(candidate_id: uuid.UUID, request: Request) -> dict:
    """Who can currently see this candidate, and who showed it to them."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        await load_visible_candidate(session, candidate_id, user_uuid, role)
        rows = (
            (
                await session.execute(
                    select(CandidateShare)
                    .where(CandidateShare.candidate_id == candidate_id)
                    .order_by(CandidateShare.created_at)
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


@router.delete("/candidates/{candidate_id}/shares/{share_id}", status_code=204)
async def delete_candidate_share(
    candidate_id: uuid.UUID, share_id: uuid.UUID, request: Request
) -> None:
    """Revokes sight, not history.

    Only the share row goes. Activities the recipient logged while they could
    see the candidate record what that person did; they are not the candidate,
    and they stay.

    The rule here is WIDER than `can_edit_candidate` — the original sharer may
    withdraw what they passed on, and a recipient may take themselves off —
    which is why it loads through the read guard and applies the check itself
    rather than going through the edit guard.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        candidate = await load_visible_candidate(
            session, candidate_id, user_uuid, role
        )
        share = (
            await session.execute(
                select(CandidateShare)
                .where(CandidateShare.id == share_id)
                .where(CandidateShare.candidate_id == candidate_id)
            )
        ).scalar_one_or_none()
        if share is None:
            raise HTTPException(status_code=404, detail="No such share.")

        allowed = (
            can_edit_candidate(candidate, user_uuid, role)  # the owner
            or share.shared_by_user_id == user_uuid  # whoever passed it on
            or share.shared_with_user_id == user_uuid  # or the recipient, leaving
        )
        if not allowed:
            raise HTTPException(
                status_code=403, detail="This share is not yours to withdraw."
            )

        await session.execute(
            delete(CandidateShare).where(CandidateShare.id == share_id)
        )


# -------------------------------------------------------- access requests


@router.post("/candidates/{candidate_id}/access-requests")
async def request_candidate_access(
    candidate_id: uuid.UUID, body: AccessRequestBody, request: Request
) -> dict[str, str]:
    """The one route deliberately reachable for a candidate you cannot see.

    It therefore does NOT call `load_visible_candidate` — that is the whole
    point of it, and `test_candidate_routes_guarded.py` exempts it by name
    with this reason. It still checks the row exists in this tenant, because a
    request against a random UUID should 404 rather than sit pending forever,
    and it returns nothing whatsoever about the row: not the name, not a
    field, not a hint.

    A second click is an upsert onto the one pending row, not a second
    request. The index that makes that work is partial — resolved rows are not
    covered — so somebody declined in March may ask again in June.
    """
    user_uuid, tenant_uuid, _role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        exists = (
            await session.execute(
                select(Candidate.id).where(Candidate.id == candidate_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=404, detail="No such candidate.")

        stmt = (
            pg_insert(CandidateAccessRequest)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                candidate_id=candidate_id,
                requested_by_user_id=user_uuid,
                status=CandidateAccessRequest.STATUS_PENDING,
                note=body.note,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "candidate_id", "requested_by_user_id"],
                index_where=_WHERE_PENDING,
                set_={"note": body.note},
            )
            .returning(CandidateAccessRequest.id)
        )
        request_id = (await session.execute(stmt)).scalar_one()
        owner_id = (
            await session.execute(
                select(Candidate.owner_id).where(Candidate.id == candidate_id)
            )
        ).scalar_one()

    await emit_candidate_event(
        CandidateEvent(
            kind=CANDIDATE_ACCESS_REQUESTED,
            tenant_id=tenant_uuid,
            candidate_id=candidate_id,
            # None on purpose: the asker cannot see this candidate, and the
            # event is denormalised into a notification body. Naming the row
            # here would leak through the notification what the route itself
            # refuses to return.
            candidate_name=None,
            # The owner, not the office. An unowned candidate has nobody to
            # ask, so it goes nowhere rather than to everyone.
            recipient_user_ids=(owner_id,) if owner_id else (),
            note=body.note,
        )
    )
    return {"id": str(request_id)}


@router.get("/candidates/access-requests")
async def list_my_access_requests(request: Request, status: str = "pending") -> dict:
    """Requests waiting on ME — the inbox.

    There is no route to mirror for this; the opportunity side never grew one
    because it has no request flow.

    Scoped to candidates the caller OWNS, so it is not a listing of every
    request in the agency. `role='owner'` sees all of them, consistently with
    every other predicate in this codebase.

    Declared before `/candidates/{candidate_id}` in `app/main.py`: FastAPI
    matches in include order, and the generic route would otherwise take
    `access-requests` for a UUID and answer 422.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    if status not in CandidateAccessRequest.STATUSES:
        raise HTTPException(status_code=422, detail="Unknown request status.")

    async with tenant_session(tenant_uuid) as session:
        mine = select(Candidate.id).where(Candidate.owner_id == user_uuid)
        query = (
            select(CandidateAccessRequest)
            .where(CandidateAccessRequest.status == status)
            .order_by(CandidateAccessRequest.created_at)
        )
        if role != OWNER_ROLE:
            query = query.where(CandidateAccessRequest.candidate_id.in_(mine))

        rows = (await session.execute(query)).scalars().all()
        items = [
            {
                "id": str(row.id),
                "candidate_id": str(row.candidate_id),
                "requested_by_user_id": str(row.requested_by_user_id),
                "status": row.status,
                "note": row.note,
                "created_at": row.created_at.isoformat(),
                "resolved_at": (
                    row.resolved_at.isoformat() if row.resolved_at else None
                ),
            }
            for row in rows
        ]

    return {"items": items}


async def _resolve(
    candidate_id: uuid.UUID,
    request_id: uuid.UUID,
    request: Request,
    *,
    grant: bool,
) -> tuple[uuid.UUID, uuid.UUID, str | None]:
    """Answer a pending request, one way or the other, in ONE transaction.

    Granting writes the share and marks the request granted together: the
    share IS the grant, and a granted request with no share would be a lie
    told to both people at once.

    Returns what the caller needs to emit after the session closes.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        candidate = await load_visible_candidate(
            session, candidate_id, user_uuid, role
        )
        if not _may_resolve(candidate, user_uuid, role):
            raise HTTPException(
                status_code=403,
                detail="Only the owner may answer a request for this candidate.",
            )
        candidate_name = candidate.full_name

        access_request = (
            await session.execute(
                select(CandidateAccessRequest)
                .where(CandidateAccessRequest.id == request_id)
                .where(CandidateAccessRequest.candidate_id == candidate_id)
                .where(
                    CandidateAccessRequest.status
                    == CandidateAccessRequest.STATUS_PENDING
                )
            )
        ).scalar_one_or_none()
        if access_request is None:
            raise HTTPException(
                status_code=404, detail="No such open request."
            )
        asker = access_request.requested_by_user_id

        if grant:
            await session.execute(
                pg_insert(CandidateShare)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_uuid,
                    candidate_id=candidate_id,
                    scope=CandidateShare.SCOPE_USER,
                    shared_with_user_id=asker,
                    shared_by_user_id=user_uuid,
                    note=access_request.note,
                )
                .on_conflict_do_update(
                    index_elements=[
                        "tenant_id",
                        "candidate_id",
                        "shared_with_user_id",
                    ],
                    index_where=_WHERE_USER_SCOPE,
                    set_={"shared_by_user_id": user_uuid},
                )
            )

        # `resolved_at` is not optional: `ck_candidate_access_requests_
        # resolution_paired` asserts `(status = 'pending') = (resolved_at IS
        # NULL)`, so a status change without a time fails at the database.
        await session.execute(
            update(CandidateAccessRequest)
            .where(CandidateAccessRequest.id == request_id)
            .values(
                status=(
                    CandidateAccessRequest.STATUS_GRANTED
                    if grant
                    else CandidateAccessRequest.STATUS_DECLINED
                ),
                resolved_at=dt.datetime.now(dt.UTC),
                resolved_by_user_id=user_uuid,
            )
        )

    return tenant_uuid, asker, candidate_name


@router.post("/candidates/{candidate_id}/access-requests/{request_id}/grant")
async def grant_candidate_access(
    candidate_id: uuid.UUID, request_id: uuid.UUID, request: Request
) -> dict[str, str]:
    tenant_uuid, asker, candidate_name = await _resolve(
        candidate_id, request_id, request, grant=True
    )

    # After commit: the share exists before the asker is told it does.
    await emit_candidate_event(
        CandidateEvent(
            kind=CANDIDATE_ACCESS_GRANTED,
            tenant_id=tenant_uuid,
            candidate_id=candidate_id,
            # Safe to name now — the share the same transaction wrote is what
            # makes this person able to read the row anyway.
            candidate_name=candidate_name,
            recipient_user_ids=(asker,),
        )
    )
    return {"status": CandidateAccessRequest.STATUS_GRANTED}


@router.post("/candidates/{candidate_id}/access-requests/{request_id}/decline")
async def decline_candidate_access(
    candidate_id: uuid.UUID, request_id: uuid.UUID, request: Request
) -> dict[str, str]:
    """Declining is not optional politeness.

    A request that silently never resolves leaves the asker believing it is
    still open, so they wait, and then they ask again. Saying no closes it.
    """
    tenant_uuid, asker, _candidate_name = await _resolve(
        candidate_id, request_id, request, grant=False
    )

    await emit_candidate_event(
        CandidateEvent(
            kind=CANDIDATE_ACCESS_DECLINED,
            tenant_id=tenant_uuid,
            candidate_id=candidate_id,
            # Still None. Being refused must not tell you what you were
            # refused — that would make a decline a cheap way to read a name.
            candidate_name=None,
            recipient_user_ids=(asker,),
        )
    )
    return {"status": CandidateAccessRequest.STATUS_DECLINED}
