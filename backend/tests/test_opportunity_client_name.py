"""The list has to say *which* client a job order is filed under.

`client_id` alone is a uuid the recruiter cannot check. The eight production
rows this feature exists to remediate name six different companies between
them, so someone linking them needs to see afterwards what they chose.

The join that resolves the name is LEFT, and that is the load-bearing part:
most of those rows are unlinked, and an INNER join would drop every one of
them from the list.
"""

import uuid

import pytest

from app.models import Client, Opportunity
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user, sign_in


async def _opportunity(
    tenant_id: uuid.UUID, *, client_id: uuid.UUID | None = None
) -> uuid.UUID:
    opportunity_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            Opportunity(
                id=opportunity_id,
                tenant_id=tenant_id,
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


@pytest.fixture
async def tenant():
    tenant_id, user_id = await seed_tenant_with_user()
    yield tenant_id, user_id
    await cleanup_tenant(tenant_id)


async def test_a_linked_job_order_carries_its_clients_name(client, tenant) -> None:
    tenant_id, mine = tenant
    client_id = await _client_row(tenant_id, "Sunrise Logistics")
    opportunity_id = await _opportunity(tenant_id, client_id=client_id)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    row = body["items"][0]
    assert row["id"] == str(opportunity_id)
    assert row["client_id"] == str(client_id)
    assert row["client_name"] == "Sunrise Logistics"


async def test_an_unlinked_job_order_is_still_listed(client, tenant) -> None:
    """The LEFT-join guard.

    Make the clients join INNER and this is the test that fails — and in
    production it would take eight of eleven rows with it.
    """
    tenant_id, mine = tenant
    opportunity_id = await _opportunity(tenant_id, client_id=None)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    assert [row["id"] for row in body["items"]] == [str(opportunity_id)]
    assert body["items"][0]["client_id"] is None
    assert body["items"][0]["client_name"] is None


async def test_linked_and_unlinked_rows_come_back_together(client, tenant) -> None:
    """The mixed page, which is what production actually looks like."""
    tenant_id, mine = tenant
    client_id = await _client_row(tenant_id, "Sunrise Logistics")
    linked = await _opportunity(tenant_id, client_id=client_id)
    unlinked = await _opportunity(tenant_id, client_id=None)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    names = {row["id"]: row["client_name"] for row in body["items"]}
    assert names == {str(linked): "Sunrise Logistics", str(unlinked): None}
    assert body["total"] == 2
