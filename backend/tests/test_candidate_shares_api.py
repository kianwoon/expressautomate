"""Sharing a candidate: the table's own guarantees, before any route exists."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.candidate_share import CandidateShare
from tests.conftest import make_candidate, make_user


@pytest.mark.asyncio
async def test_a_tenant_share_must_have_no_recipient(admin_session, seeded) -> None:
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-share-pairing")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_TENANT,
            shared_with_user_id=owner,  # illegal: a broadcast names nobody
        )
    )
    with pytest.raises(IntegrityError):
        await admin_session.flush()


@pytest.mark.asyncio
async def test_deleting_a_recipient_deletes_their_shares(admin_session, seeded) -> None:
    """CASCADE, not SET NULL. SET NULL would turn a targeted share into a
    broadcast and violate ck_candidate_shares_scope_target, making the user
    DELETE fail outright."""
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-share-cascade")
    recipient = await make_user(admin_session, tenant_id, "colleague@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=recipient,
            shared_by_user_id=owner,
        )
    )
    await admin_session.commit()

    await admin_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": recipient})
    await admin_session.commit()

    left = (
        await admin_session.execute(
            text("SELECT count(*) AS n FROM candidate_shares WHERE candidate_id = :c"),
            {"c": candidate_id},
        )
    ).one()
    assert left.n == 0


@pytest.mark.asyncio
async def test_a_share_cannot_reach_another_agency(admin_session, seeded) -> None:
    """Refused by the composite foreign key, not merely by application code."""
    make_tenant, _, _ = seeded
    tenant_a, owner_a, _ = await make_tenant("agency-a-cross")
    tenant_b, owner_b, _ = await make_tenant("agency-b-cross")
    candidate_id = await make_candidate(admin_session, tenant_a, owner_id=owner_a)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_a,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=owner_b,  # a user in the other agency
            shared_by_user_id=owner_a,
        )
    )
    with pytest.raises(IntegrityError):
        await admin_session.flush()


@pytest.mark.asyncio
async def test_only_one_pending_request_per_person(admin_session, seeded) -> None:
    """A recruiter clicking twice must not spam the owner."""
    from app.models.candidate_access_request import CandidateAccessRequest

    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-request-dedupe")
    asker = await make_user(admin_session, tenant_id, "asker@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    for _ in range(2):
        admin_session.add(
            CandidateAccessRequest(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                requested_by_user_id=asker,
                status=CandidateAccessRequest.STATUS_PENDING,
            )
        )
    with pytest.raises(IntegrityError):
        await admin_session.flush()
