"""Who, inside one agency, may see and edit a candidate."""


import uuid

import pytest
from sqlalchemy import select, text

from app.models.candidate import Candidate
from tests.conftest import make_candidate, make_user


@pytest.mark.asyncio
async def test_candidate_has_an_owner_column(admin_session) -> None:
    row = (
        await admin_session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'candidates' AND column_name = 'owner_id'"
            )
        )
    ).first()
    assert row is not None, "candidates.owner_id does not exist"
    assert row.is_nullable == "YES", "owner_id must be nullable — NULL is the queue"


@pytest.mark.asyncio
async def test_deleting_a_recruiter_releases_their_candidates(admin_session, seeded) -> None:
    """The column-qualified SET NULL. A bare SET NULL nulls tenant_id, which is
    NOT NULL, so deleting a recruiter would fail outright."""
    make_tenant, _make_opportunity, _make_evidence = seeded
    tenant_id, recruiter, _mailbox = await make_tenant("agency-owner-release")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=recruiter)
    await admin_session.commit()

    await admin_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": recruiter})
    await admin_session.commit()

    row = (
        await admin_session.execute(
            select(Candidate.owner_id, Candidate.tenant_id).where(Candidate.id == candidate_id)
        )
    ).one()
    assert row.owner_id is None
    assert row.tenant_id == tenant_id, "tenant_id was nulled — the SET NULL is not column-qualified"


from app.models.candidate_share import CandidateShare
from app.services.visibility import can_edit_candidate, visible_candidates


@pytest.mark.asyncio
async def test_predicate_terms(admin_session, seeded) -> None:
    """Each term, one at a time. A leak is usually one term too wide."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-predicate")
    colleague = await make_user(admin_session, tenant_id, "colleague@agency.test")

    mine = await make_candidate(admin_session, tenant_id, owner_id=me)
    theirs = await make_candidate(admin_session, tenant_id, owner_id=colleague)
    queued = await make_candidate(admin_session, tenant_id, owner_id=None)
    shared = await make_candidate(admin_session, tenant_id, owner_id=colleague)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=shared,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=me,
            shared_by_user_id=colleague,
        )
    )
    await admin_session.commit()

    visible = set(
        (
            await admin_session.execute(
                select(Candidate.id).where(visible_candidates(me, "recruiter"))
            )
        )
        .scalars()
        .all()
    )
    assert mine in visible
    assert queued in visible, "the unclaimed queue must be conspicuous"
    assert shared in visible
    assert theirs not in visible, "a colleague's private candidate leaked"

    everything = set(
        (
            await admin_session.execute(
                select(Candidate.id).where(visible_candidates(me, "owner"))
            )
        )
        .scalars()
        .all()
    )
    assert theirs in everything, "role=owner must see the whole database"


def test_an_unowned_candidate_is_visible_but_not_editable() -> None:
    """Claiming is the act that creates edit rights."""
    unowned = Candidate(id=uuid.uuid4(), full_name="Wei Ming", owner_id=None)
    assert can_edit_candidate(unowned, uuid.uuid4(), "recruiter") is False
    assert can_edit_candidate(unowned, uuid.uuid4(), "owner") is True
