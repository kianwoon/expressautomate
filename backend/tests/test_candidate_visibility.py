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
