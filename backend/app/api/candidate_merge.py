"""Combining two records of one human being, and taking that back.

Lifted verbatim out of `candidates.py`, which had reached the 1500-line ceiling
this project sets. Nothing about the behaviour changed: the same two routes,
the same lock ordering, the same guards.

The dependency runs one way only. This module imports `_load` and `_serialize`
from `candidates`; `candidates` must never import this one, or the two files
form a cycle at import time.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, text, update

from app.api.auth import _require_session_with_role
from app.db.rls import tenant_session
from app.models.candidate import Candidate
from app.services.visibility import load_editable_candidate

router = APIRouter(tags=["candidates"])


class MergeRequest(BaseModel):
    target_id: uuid.UUID


async def _lock_pair(session, first: uuid.UUID, second: uuid.UUID) -> None:
    """Take a row lock on both candidates, lowest id first.

    One statement per row rather than an `IN (...)` with an ORDER BY: the order
    in which Postgres locks the rows of a single statement is a property of the
    chosen plan, not of the ORDER BY, so only separate statements make the
    ordering something this code actually decides. A missing row simply locks
    nothing — the 404 comes from `load_editable_candidate` afterwards.
    """
    for candidate_id in sorted((first, second), key=lambda value: value.bytes):
        await session.execute(
            select(Candidate.id).where(Candidate.id == candidate_id).with_for_update()
        )


@router.post("/candidates/{candidate_id}/merge")
async def merge_candidate(
    request: Request, candidate_id: uuid.UUID, body: MergeRequest
) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    if body.target_id == candidate_id:
        raise HTTPException(status_code=400, detail="A candidate cannot be merged into itself")

    async with tenant_session(tenant_uuid) as session:
        # Lock both rows before reading their statuses, or two opposing merges
        # (A→B and B→A at the same instant) each validate against a snapshot
        # taken before the other wrote, both succeed, and the two rows end up
        # pointing at each other: a cycle in which neither row is live, neither
        # appears in any list, and neither can be unmerged into a live record.
        #
        # Locked in a fixed order — lowest id first — so the two transactions
        # queue rather than deadlock. The statuses are only read *after* both
        # locks are held, because the pre-lock read is exactly what is stale.
        await _lock_pair(session, candidate_id, body.target_id)
        # Merging is destructive on one side and additive on the other, so the
        # caller must hold both. The realistic case — B discovers, after being
        # granted access, that they and A hold the same person — is not a
        # merge B performs. Until a cross-owner merge request exists,
        # `role='owner'` is the escape hatch, which is workable in an agency
        # where the boss is one desk away.
        loser = await load_editable_candidate(session, candidate_id, user_uuid, role)
        target = await load_editable_candidate(session, body.target_id, user_uuid, role)
        if target.record_status == Candidate.MERGED:
            # Chains would need every reader to walk them. Refusing here is
            # what keeps the graph one hop deep.
            raise HTTPException(
                status_code=400, detail="Target is itself merged; merge into its target"
            )
        if loser.record_status == Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Candidate is already merged")

        # Someone may already point at the row we're about to merge. Refusing
        # would strand a recruiter who has three duplicates of one person and
        # no way to combine them; re-pointing those rows at the new target
        # keeps the graph one hop deep instead of forming a chain, and stays
        # inside this transaction so it commits or rolls back with the rest.
        await session.execute(
            update(Candidate)
            .where(
                Candidate.merged_into_candidate_id == candidate_id,
                Candidate.record_status == Candidate.MERGED,
            )
            .values(merged_into_candidate_id=body.target_id)
        )

        # Skills that the target already has would violate the per-candidate
        # unique key, so move only the ones it lacks and drop the rest — a
        # duplicate skill carries no information the target does not have.
        await session.execute(
            text(
                """
                DELETE FROM candidate_skills loser
                WHERE loser.candidate_id = :loser
                  AND EXISTS (
                      SELECT 1 FROM candidate_skills t
                      WHERE t.candidate_id = :target
                        AND t.skill_normalized = loser.skill_normalized
                  )
                """
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        await session.execute(
            text(
                "UPDATE candidate_skills SET candidate_id = :target WHERE candidate_id = :loser"
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        # Languages move exactly as skills do, and for the same reason: the
        # loser row is a duplicate record of the same human being, so what it
        # knows about them belongs on the survivor. Left behind they would sit
        # on a row nothing reads, invisible until the merged row is deleted.
        await session.execute(
            text(
                """
                DELETE FROM candidate_languages loser
                WHERE loser.candidate_id = :loser
                  AND EXISTS (
                      SELECT 1 FROM candidate_languages t
                      WHERE t.candidate_id = :target
                        AND t.language_normalized = loser.language_normalized
                  )
                """
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        await session.execute(
            text(
                "UPDATE candidate_languages SET candidate_id = :target "
                "WHERE candidate_id = :loser"
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        # Overrides move the same way, and for the same reason: they are a
        # record of what a person decided about this human being.
        await session.execute(
            text(
                """
                DELETE FROM candidate_field_overrides loser
                WHERE loser.candidate_id = :loser
                  AND EXISTS (
                      SELECT 1 FROM candidate_field_overrides t
                      WHERE t.candidate_id = :target AND t.field_name = loser.field_name
                  )
                """
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        await session.execute(
            text(
                "UPDATE candidate_field_overrides SET candidate_id = :target "
                "WHERE candidate_id = :loser"
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        # Status and target in one statement — a CHECK enforces that a merged
        # row names its target and a live row does not.
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(
                record_status=Candidate.MERGED, merged_into_candidate_id=body.target_id
            )
        )
        await session.commit()
    return {"record_status": Candidate.MERGED, "merged_into_candidate_id": str(body.target_id)}


@router.post("/candidates/{candidate_id}/unmerge")
async def unmerge_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    """Restore a merged candidate. Skills and overrides stay with the target.

    Deliberately partial: a moved row carries no record of which candidate it
    came from, so it cannot be given back. The identity keys return, which is
    what makes the person findable again.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_editable_candidate(session, candidate_id, user_uuid, role)
        if candidate.record_status != Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Candidate is not merged")

        # Unmerging returns this row's email and phone to the live indexes. If
        # somebody else took either in the meantime, restoring would violate a
        # unique index — so say who holds it rather than 500.
        clash = (
            await session.execute(
                text(
                    """
                    SELECT id, full_name FROM candidates
                    WHERE record_status <> 'merged' AND id <> :id
                      AND ((CAST(:email AS text) IS NOT NULL
                            AND lower(email) = lower(CAST(:email AS text)))
                        OR (CAST(:phone AS text) IS NOT NULL
                            AND phone_e164 = CAST(:phone AS text)))
                    LIMIT 1
                    """
                ),
                {"id": candidate_id, "email": candidate.email, "phone": candidate.phone_e164},
            )
        ).first()
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot unmerge: {clash.full_name} ({clash.id}) now holds "
                    "this candidate's email or phone."
                ),
            )

        # `owner_id` is deliberately not written here. It survived the merge on
        # this row, so reviving the row restores its original owner. If that
        # recruiter has since been deleted the column is already NULL and the
        # row lands in the queue — the same outcome every other path gives.
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(record_status=Candidate.ACTIVE, merged_into_candidate_id=None)
        )
        await session.commit()
    return {"record_status": Candidate.ACTIVE}
