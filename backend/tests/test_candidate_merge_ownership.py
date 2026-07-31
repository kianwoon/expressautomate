"""Merging destroys one of two records. Both must be yours to destroy."""

import pytest
from sqlalchemy import text

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
