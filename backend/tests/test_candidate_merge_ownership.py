"""Merging destroys one of two records. Both must be yours to destroy."""

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from app.models.candidate_access_request import CandidateAccessRequest
from app.models.candidate_share import CandidateShare
from tests.conftest import make_candidate, make_user, sign_in


@pytest.mark.asyncio
async def test_merge_needs_edit_rights_on_both_sides(client, admin_session, seeded) -> None:
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-merge-rights")
    colleague = await make_user(admin_session, tenant_id, "mc@agency.test")
    mine = await make_candidate(admin_session, tenant_id, owner_id=me)
    theirs = await make_candidate(admin_session, tenant_id, owner_id=colleague)
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        f"/api/candidates/{mine}/merge", json={"target_id": str(theirs)}
    )
    assert response.status_code in (403, 404)

    still_there = (
        await admin_session.execute(
            text("SELECT record_status FROM candidates WHERE id = :id"), {"id": mine}
        )
    ).one()
    assert still_there.record_status == "active"


@pytest.mark.asyncio
async def test_unmerge_restores_the_original_owner(client, admin_session, seeded) -> None:
    """The revived row goes back to whoever held it, not to whoever pressed
    the button.

    The presser is a colleague, not the original owner: `role='owner'` gets
    them past the edit guard (the documented escape hatch for a cross-owner
    unmerge), while the merged row itself stays owned by someone else. That
    way `owner_id == user_uuid` and `owner_id == original_owner` disagree, so
    the assertion below only holds if the code truly leaves `owner_id` alone.
    """
    make_tenant, _, _ = seeded
    tenant_id, original_owner, _ = await make_tenant("agency-unmerge-owner")
    presser = await make_user(admin_session, tenant_id, "boss@agency.test", role="owner")
    target = await make_candidate(admin_session, tenant_id, owner_id=original_owner)
    merged = await make_candidate(
        admin_session,
        tenant_id,
        owner_id=original_owner,
        record_status="merged",
        merged_into_candidate_id=target,
    )
    await admin_session.commit()

    sign_in(client, presser, tenant_id)
    assert (await client.post(f"/api/candidates/{merged}/unmerge")).status_code == 200

    row = (
        await admin_session.execute(
            text("SELECT owner_id, record_status FROM candidates WHERE id = :id"), {"id": merged}
        )
    ).one()
    assert row.record_status == "active"
    assert row.owner_id == original_owner, (
        "unmerge reassigned the row to whoever pressed the button"
    )


@pytest.mark.asyncio
async def test_merge_moves_user_share_from_loser_to_target(
    client, admin_session, seeded
) -> None:
    """A colleague with sight of the loser must keep sight of the survivor."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-merge-share-user")
    colleague = await make_user(admin_session, tenant_id, "colleague@agency.test")
    loser = await make_candidate(admin_session, tenant_id, owner_id=me)
    target = await make_candidate(admin_session, tenant_id, owner_id=me)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=loser,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=colleague,
            shared_by_user_id=me,
        )
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        f"/api/candidates/{loser}/merge", json={"target_id": str(target)}
    )
    assert response.status_code == 200

    row = (
        await admin_session.execute(
            text(
                "SELECT candidate_id FROM candidate_shares "
                "WHERE tenant_id = :t AND shared_with_user_id = :u"
            ),
            {"t": tenant_id, "u": colleague},
        )
    ).one()
    assert row.candidate_id == target

    # The row moving is not the point; the colleague still finding the person
    # is. Read it back through the API as them.
    sign_in(client, colleague, tenant_id)
    seen = await client.get(f"/api/candidates/{target}")
    assert seen.status_code == 200, "the colleague lost sight of the person at merge"


@pytest.mark.asyncio
async def test_merge_moves_tenant_broadcast_share_from_loser_to_target(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-merge-share-tenant")
    loser = await make_candidate(admin_session, tenant_id, owner_id=me)
    target = await make_candidate(admin_session, tenant_id, owner_id=me)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=loser,
            scope=CandidateShare.SCOPE_TENANT,
            shared_by_user_id=me,
        )
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        f"/api/candidates/{loser}/merge", json={"target_id": str(target)}
    )
    assert response.status_code == 200

    row = (
        await admin_session.execute(
            text(
                "SELECT candidate_id, scope FROM candidate_shares WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
    ).one()
    assert row.candidate_id == target
    assert row.scope == CandidateShare.SCOPE_TENANT


@pytest.mark.asyncio
async def test_merge_collisions_are_dropped_without_error(
    client, admin_session, seeded
) -> None:
    """Same colleague / broadcast / requester on both rows must not raise."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-merge-collision")
    colleague = await make_user(admin_session, tenant_id, "dup-colleague@agency.test")
    asker = await make_user(admin_session, tenant_id, "dup-asker@agency.test")
    loser = await make_candidate(admin_session, tenant_id, owner_id=me)
    target = await make_candidate(admin_session, tenant_id, owner_id=me)

    admin_session.add_all(
        [
            CandidateShare(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=loser,
                scope=CandidateShare.SCOPE_USER,
                shared_with_user_id=colleague,
                shared_by_user_id=me,
            ),
            CandidateShare(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=target,
                scope=CandidateShare.SCOPE_USER,
                shared_with_user_id=colleague,
                shared_by_user_id=me,
            ),
            CandidateShare(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=loser,
                scope=CandidateShare.SCOPE_TENANT,
                shared_by_user_id=me,
            ),
            CandidateShare(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=target,
                scope=CandidateShare.SCOPE_TENANT,
                shared_by_user_id=me,
            ),
            CandidateAccessRequest(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=loser,
                requested_by_user_id=asker,
                status=CandidateAccessRequest.STATUS_PENDING,
            ),
            CandidateAccessRequest(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=target,
                requested_by_user_id=asker,
                status=CandidateAccessRequest.STATUS_PENDING,
            ),
        ]
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        f"/api/candidates/{loser}/merge", json={"target_id": str(target)}
    )
    assert response.status_code == 200

    share_count = (
        await admin_session.execute(
            text(
                "SELECT count(*) FROM candidate_shares WHERE tenant_id = :t "
                "AND shared_with_user_id = :u"
            ),
            {"t": tenant_id, "u": colleague},
        )
    ).scalar_one()
    assert share_count == 1

    broadcast_count = (
        await admin_session.execute(
            text(
                "SELECT count(*) FROM candidate_shares WHERE tenant_id = :t "
                "AND scope = 'tenant'"
            ),
            {"t": tenant_id},
        )
    ).scalar_one()
    assert broadcast_count == 1

    pending_count = (
        await admin_session.execute(
            text(
                "SELECT count(*) FROM candidate_access_requests WHERE tenant_id = :t "
                "AND requested_by_user_id = :u AND status = 'pending'"
            ),
            {"t": tenant_id, "u": asker},
        )
    ).scalar_one()
    assert pending_count == 1


@pytest.mark.asyncio
async def test_merge_moves_pending_request_and_grant_lands_on_target(
    client, admin_session, seeded
) -> None:
    """A pending request on the loser must be actionable, and granting it
    must produce a share on the TARGET, not the tombstoned loser."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-merge-pending-grant")
    asker = await make_user(admin_session, tenant_id, "pending-asker@agency.test")
    loser = await make_candidate(admin_session, tenant_id, owner_id=me)
    target = await make_candidate(admin_session, tenant_id, owner_id=me)
    request_id = uuid.uuid4()
    admin_session.add(
        CandidateAccessRequest(
            id=request_id,
            tenant_id=tenant_id,
            candidate_id=loser,
            requested_by_user_id=asker,
            status=CandidateAccessRequest.STATUS_PENDING,
        )
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    merge_response = await client.post(
        f"/api/candidates/{loser}/merge", json={"target_id": str(target)}
    )
    assert merge_response.status_code == 200

    grant_response = await client.post(
        f"/api/candidates/{target}/access-requests/{request_id}/grant"
    )
    assert grant_response.status_code == 200

    row = (
        await admin_session.execute(
            text(
                "SELECT candidate_id FROM candidate_shares "
                "WHERE tenant_id = :t AND shared_with_user_id = :u"
            ),
            {"t": tenant_id, "u": asker},
        )
    ).one()
    assert row.candidate_id == target


@pytest.mark.asyncio
async def test_merge_leaves_resolved_requests_pointing_at_loser(
    client, admin_session, seeded
) -> None:
    """Resolved requests are history against the record a decision was made
    on; merge must not repoint them."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-merge-resolved")
    asker = await make_user(admin_session, tenant_id, "resolved-asker@agency.test")
    loser = await make_candidate(admin_session, tenant_id, owner_id=me)
    target = await make_candidate(admin_session, tenant_id, owner_id=me)
    request_id = uuid.uuid4()
    admin_session.add(
        CandidateAccessRequest(
            id=request_id,
            tenant_id=tenant_id,
            candidate_id=loser,
            requested_by_user_id=asker,
            status=CandidateAccessRequest.STATUS_GRANTED,
            resolved_at=dt.datetime.now(dt.UTC),
            resolved_by_user_id=me,
        )
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        f"/api/candidates/{loser}/merge", json={"target_id": str(target)}
    )
    assert response.status_code == 200

    row = (
        await admin_session.execute(
            text(
                "SELECT candidate_id FROM candidate_access_requests WHERE id = :id"
            ),
            {"id": request_id},
        )
    ).one()
    assert row.candidate_id == loser


@pytest.mark.asyncio
async def test_unmerge_does_not_revert_moved_shares(
    client, admin_session, seeded
) -> None:
    """Shares moved to the survivor at merge time stay there after unmerge —
    they now legitimately belong to the survivor, documented in
    `unmerge_candidate`."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-unmerge-share")
    colleague = await make_user(admin_session, tenant_id, "unmerge-colleague@agency.test")
    loser = await make_candidate(admin_session, tenant_id, owner_id=me)
    target = await make_candidate(admin_session, tenant_id, owner_id=me)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=loser,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=colleague,
            shared_by_user_id=me,
        )
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    assert (
        await client.post(f"/api/candidates/{loser}/merge", json={"target_id": str(target)})
    ).status_code == 200
    assert (await client.post(f"/api/candidates/{loser}/unmerge")).status_code == 200

    row = (
        await admin_session.execute(
            text(
                "SELECT candidate_id FROM candidate_shares "
                "WHERE tenant_id = :t AND shared_with_user_id = :u"
            ),
            {"t": tenant_id, "u": colleague},
        )
    ).one()
    assert row.candidate_id == target, (
        "unmerge must not revert a share that moved to the target at merge time"
    )
