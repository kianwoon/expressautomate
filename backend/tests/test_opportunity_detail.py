"""One job order, read back by id.

The list is not enough. Every write in the panel — claiming, assigning,
linking a client, setting a placement type — answers with the few fields it
changed, so the panel re-reads the whole row afterwards to replace what it is
showing. Without this route that re-read 404s, the browser swallows it, and
the screen only catches up on the next poll.

The shape is the list's shape, asserted here by comparing the two rather than
by listing keys: a row that renders one way in the table and another way in
the panel is the bug this route would otherwise introduce.
"""

import uuid

import pytest

from app.models import Client, Opportunity, User
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user, sign_in


async def _opportunity(
    tenant_id: uuid.UUID,
    *,
    assigned_user_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
) -> uuid.UUID:
    opportunity_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            Opportunity(
                id=opportunity_id,
                tenant_id=tenant_id,
                assigned_user_id=assigned_user_id,
                client_id=client_id,
                company_name_raw="Acme Pte Ltd",
                job_title_raw="Java Developer",
                source=Opportunity.MANUAL,
            )
        )
        await s.commit()
    return opportunity_id


async def _client_row(tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    client_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            Client(
                id=client_id,
                tenant_id=tenant_id,
                name=name,
                name_normalized=name.lower(),
            )
        )
        await s.commit()
    return client_id


async def _colleague(tenant_id: uuid.UUID, *, preferred_name: str | None = None) -> uuid.UUID:
    other = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            User(
                id=other,
                tenant_id=tenant_id,
                email=f"{other.hex[:8]}@agency.sg",
                preferred_name=preferred_name,
            )
        )
        await s.commit()
    return other


@pytest.fixture
async def tenant():
    tenant_id, user_id = await seed_tenant_with_user()
    yield tenant_id, user_id
    await cleanup_tenant(tenant_id)


async def test_one_job_order_reads_back_exactly_as_the_list_renders_it(client, tenant) -> None:
    """The whole reason the route exists: the panel replaces a list row with
    what this returns, so any difference between the two is a row that
    changes appearance for no reason the recruiter can see."""
    tenant_id, mine = tenant
    async with AdminSessionLocal() as s:
        u = await s.get(User, mine)
        u.preferred_name = "Mei Ling"
        await s.commit()
    client_id = await _client_row(tenant_id, "Sunrise Logistics")
    opportunity_id = await _opportunity(
        tenant_id, assigned_user_id=mine, client_id=client_id
    )

    sign_in(client, mine, tenant_id)
    listed = (await client.get("/api/opportunities")).json()["items"][0]
    response = await client.get(f"/api/opportunities/{opportunity_id}")

    assert response.status_code == 200
    assert response.json() == listed
    assert listed["assignee_name"] == "Mei Ling"
    assert listed["client_name"] == "Sunrise Logistics"


async def test_the_placement_fields_come_back(client, tenant) -> None:
    """What the placement form re-reads after it saves. Absent from the
    payload, the form redraws itself as `Not set` over a value that is set."""
    tenant_id, mine = tenant
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    sign_in(client, mine, tenant_id)

    await client.post(
        f"/api/opportunities/{opportunity_id}/placement-type",
        json={"placement_type": "mdw_work_permit"},
    )
    await client.post(
        f"/api/opportunities/{opportunity_id}/occupational-requirement",
        json={
            "sex_requirement": "female",
            "sex_requirement_reason": "Intimate personal care for an elderly client.",
        },
    )

    row = (await client.get(f"/api/opportunities/{opportunity_id}")).json()

    assert row["placement_type"] == "mdw_work_permit"
    assert row["sex_requirement"] == "female"
    assert row["sex_requirement_reason"] == (
        "Intimate personal care for an elderly client."
    )


async def test_the_list_carries_the_placement_fields_too(client, tenant) -> None:
    """Same fields, same names, in the list — the panel opens on a list row
    before it ever re-reads one."""
    tenant_id, mine = tenant
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    sign_in(client, mine, tenant_id)
    await client.post(
        f"/api/opportunities/{opportunity_id}/placement-type",
        json={"placement_type": "local_hire"},
    )

    row = (await client.get("/api/opportunities")).json()["items"][0]

    assert row["placement_type"] == "local_hire"
    assert row["sex_requirement"] is None
    assert row["sex_requirement_reason"] is None


async def test_another_agencys_job_order_is_a_404(client, tenant) -> None:
    tenant_a, mine = tenant
    tenant_b, theirs = await seed_tenant_with_user()
    try:
        opportunity_id = await _opportunity(tenant_b, assigned_user_id=theirs)
        sign_in(client, mine, tenant_a)
        response = await client.get(f"/api/opportunities/{opportunity_id}")
        assert response.status_code == 404
    finally:
        await cleanup_tenant(tenant_b)


async def test_a_colleagues_own_job_order_is_a_404_not_a_403(client, tenant) -> None:
    """404, deliberately. A 403 would confirm the row exists, which is the one
    thing an invisible row must not tell you."""
    tenant_id, mine = tenant
    colleague = await _colleague(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=colleague)

    sign_in(client, mine, tenant_id)
    response = await client.get(f"/api/opportunities/{opportunity_id}")

    assert response.status_code == 404


async def test_signed_out_is_a_401(client, tenant) -> None:
    tenant_id, mine = tenant
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    response = await client.get(f"/api/opportunities/{opportunity_id}")
    assert response.status_code == 401
