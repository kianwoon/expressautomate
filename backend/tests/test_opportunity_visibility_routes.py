"""The visibility predicate as a recruiter experiences it.

The structural tests in `test_opportunity_routes_guarded.py` prove a guard is
*called*; these prove it decides the right thing. Kept apart because the two
fail for different reasons: a structural failure means someone added a route,
a failure here means the rule itself moved.
"""

import uuid

import pytest
from sqlalchemy import text

from app.models import User
from app.models.opportunity_share import OpportunityShare
from tests.conftest import AdminSessionLocal, sign_in


async def _colleague(tenant_id: uuid.UUID) -> uuid.UUID:
    """A second recruiter in the same agency."""
    other = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(User(id=other, tenant_id=tenant_id, email=f"{other.hex[:8]}@agency.sg"))
        await s.commit()
    return other


async def _share_with(opportunity_id: uuid.UUID, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        s.add(
            OpportunityShare(
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                scope=OpportunityShare.SCOPE_USER,
                shared_with_user_id=user_id,
            )
        )
        await s.commit()


@pytest.fixture
async def colleagues_job_order(seeded):
    """A job order assigned to somebody else in the same agency."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    other = await _colleague(tenant_id)
    # The mailbox belongs to `user_id`, and the predicate deliberately lets the
    # recipient of the original mail keep sight of what was extracted from it —
    # so the email is re-pointed at a mailbox of the colleague's, otherwise this
    # row would be visible for a reason that has nothing to do with assignment.
    other_mailbox = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO mailboxes"
                " (id, tenant_id, user_id, ms_user_id, scope, folder_id, retention_months)"
                " VALUES (:i, :t, :u, :m, 'user', 'inbox', 12)"
            ),
            {"i": other_mailbox, "t": tenant_id, "u": other, "m": f"oid-{other.hex[:8]}"},
        )
        await s.commit()
    opportunity_id = await make_opportunity(
        tenant_id, other_mailbox, company_name_raw="Acme Pte Ltd", assigned_user_id=other
    )
    _ = mailbox_id
    return tenant_id, user_id, other, opportunity_id


async def test_a_colleagues_job_order_is_not_in_the_list(client, colleagues_job_order) -> None:
    tenant_id, user_id, _other, _opportunity_id = colleagues_job_order
    sign_in(client, user_id, tenant_id)

    body = (await client.get("/api/opportunities")).json()

    assert body["items"] == []
    # The count must agree with the page, or the list says twelve and shows four.
    assert body["counts"]["all"] == 0
    assert body["total"] == 0


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("review", {"reviewed": True}),
        ("placement-type", {"placement_type": "local_hire"}),
        (
            "occupational-requirement",
            {"sex_requirement": "female", "sex_requirement_reason": "live-in care"},
        ),
    ],
)
async def test_writing_to_a_colleagues_job_order_is_a_404(
    client, colleagues_job_order, path, payload
) -> None:
    """404, not 403 — a 403 would confirm the id exists."""
    tenant_id, user_id, _other, opportunity_id = colleagues_job_order
    sign_in(client, user_id, tenant_id)

    response = await client.post(f"/api/opportunities/{opportunity_id}/{path}", json=payload)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "No such job order."


async def test_setting_placement_type_on_your_own_job_order_succeeds(client, seeded) -> None:
    """The positive case for the guard: an ordinary recruiter (not owner)
    who IS the assignee can write `placement_type`, and the audit column
    records them, not a stand-in owner. `test_eligibility_api.py` and
    `test_candidates_eligible_filter.py` only ever exercise this route
    through an owner fixture, which never reaches `can_edit`'s non-owner
    branch."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Acme Pte Ltd", assigned_user_id=user_id
    )

    sign_in(client, user_id, tenant_id)
    response = await client.post(
        f"/api/opportunities/{opportunity_id}/placement-type",
        json={"placement_type": "local_hire"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"id": str(opportunity_id), "placement_type": "local_hire"}

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT placement_type, placement_type_set_by"
                    " FROM opportunities WHERE id = :i"
                ),
                {"i": opportunity_id},
            )
        ).one()
    assert row.placement_type == "local_hire"
    assert row.placement_type_set_by == user_id


async def test_setting_occupational_requirement_on_your_own_job_order_succeeds(
    client, seeded
) -> None:
    """Same positive case, for the other regulatory route. The requirement
    and its reason must be supplied together (the CHECK constraint enforces
    the pairing), and both persist along with the acting user."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, user_id, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Acme Pte Ltd", assigned_user_id=user_id
    )

    sign_in(client, user_id, tenant_id)
    response = await client.post(
        f"/api/opportunities/{opportunity_id}/occupational-requirement",
        json={"sex_requirement": "female", "sex_requirement_reason": "live-in care"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": str(opportunity_id),
        "sex_requirement": "female",
        "sex_requirement_reason": "live-in care",
    }

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT sex_requirement, sex_requirement_reason, sex_requirement_set_by"
                    " FROM opportunities WHERE id = :i"
                ),
                {"i": opportunity_id},
            )
        ).one()
    assert row.sex_requirement == "female"
    assert row.sex_requirement_reason == "live-in care"
    assert row.sex_requirement_set_by == user_id


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("review", {"reviewed": True}),
        ("placement-type", {"placement_type": "local_hire"}),
        (
            "occupational-requirement",
            {"sex_requirement": "female", "sex_requirement_reason": "live-in care"},
        ),
    ],
)
async def test_a_share_is_not_permission_to_edit(
    client, colleagues_job_order, path, payload
) -> None:
    """The point of the whole feature: `placement_type_set_by` and
    `sex_requirement_set_by` record a regulatory judgement, and being shown a
    job order is not being given it."""
    tenant_id, user_id, _other, opportunity_id = colleagues_job_order
    await _share_with(opportunity_id, tenant_id, user_id)
    sign_in(client, user_id, tenant_id)

    listed = (await client.get("/api/opportunities")).json()
    assert [item["id"] for item in listed["items"]] == [str(opportunity_id)]

    response = await client.post(f"/api/opportunities/{opportunity_id}/{path}", json=payload)
    assert response.status_code == 403, response.text


async def test_eligibility_on_a_colleagues_job_order_is_a_404(
    client, colleagues_job_order
) -> None:
    """The read guard, on the one route that reads a job order by id without
    writing it. The candidate id is unused — the job order is refused first."""
    tenant_id, user_id, _other, opportunity_id = colleagues_job_order
    sign_in(client, user_id, tenant_id)

    response = await client.get(
        f"/api/opportunities/{opportunity_id}/candidates/{uuid.uuid4()}/eligibility"
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "No such job order."


async def test_assigning_to_a_real_user_in_another_tenant_is_refused(client, seeded) -> None:
    """`assign_opportunity` looks the target user up with `select(User.id)`
    inside the caller's `tenant_session`, so RLS scopes that query to the
    caller's own agency. A user who is real, but belongs to a different
    tenant, is therefore simply not found — the same 422 the stranger-id case
    gets, not a 500 from the composite FK and not a leak of the other
    tenant's user id via a distinguishable error."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_a, user_a, mailbox_a = await make_tenant("agency-a")
    tenant_b, user_b, _mailbox_b = await make_tenant("agency-b")
    opportunity_id = await make_opportunity(
        tenant_a, mailbox_a, company_name_raw="Acme Pte Ltd", assigned_user_id=user_a
    )
    sign_in(client, user_a, tenant_a)

    response = await client.post(
        f"/api/opportunities/{opportunity_id}/assign",
        json={"user_id": str(user_b)},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "That colleague is not in this agency."
