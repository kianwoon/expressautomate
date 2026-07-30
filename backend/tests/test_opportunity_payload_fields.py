"""The list has to say who owns a row, or the interface cannot show it."""

import uuid

import pytest

from app.models import Opportunity, User
from app.models.opportunity_share import OpportunityShare
from tests import test_opportunities_api as _api
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user

client = _api.client
sign_in = _api.sign_in


async def _opportunity(
    tenant_id: uuid.UUID, *, assigned_user_id: uuid.UUID | None = None
) -> uuid.UUID:
    """A job order with no email behind it — typed in by hand, hence `source`
    defaults to 'manual' rather than the pipeline's 'pipeline'."""
    opportunity_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            Opportunity(
                id=opportunity_id,
                tenant_id=tenant_id,
                assigned_user_id=assigned_user_id,
                company_name_raw="Acme Pte Ltd",
                job_title_raw="Java Developer",
                source=Opportunity.MANUAL,
            )
        )
        await s.commit()
    return opportunity_id


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


async def _share_with(
    tenant_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    tenant_scope: bool = False,
) -> None:
    async with AdminSessionLocal() as s:
        s.add(
            OpportunityShare(
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                scope=(
                    OpportunityShare.SCOPE_TENANT
                    if tenant_scope
                    else OpportunityShare.SCOPE_USER
                ),
                shared_with_user_id=None if tenant_scope else user_id,
            )
        )
        await s.commit()


@pytest.fixture
async def tenant():
    tenant_id, user_id = await seed_tenant_with_user()
    yield tenant_id, user_id
    await cleanup_tenant(tenant_id)


async def test_the_list_reports_the_assignee_and_their_name(client, tenant) -> None:
    tenant_id, mine = tenant
    async with AdminSessionLocal() as s:
        u = await s.get(User, mine)
        u.preferred_name = "Mei Ling"
        await s.commit()
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    _ = opportunity_id

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    row = body["items"][0]
    assert row["assigned_user_id"] == str(mine)
    assert row["assignee_name"] == "Mei Ling"
    assert row["source"] == "manual"
    assert row["shared_with_me"] is False


async def test_a_whitespace_only_preferred_name_falls_through_to_display_name(
    client, tenant
) -> None:
    """A blank `preferred_name` must not out-rank a real `display_name` — the
    same rule `app/api/clients.py::_assignee_name_expr` enforces, so the same
    recruiter renders identically on both screens."""
    tenant_id, mine = tenant
    async with AdminSessionLocal() as s:
        u = await s.get(User, mine)
        u.preferred_name = "   "
        u.display_name = "Mei Ling Tan"
        await s.commit()
    await _opportunity(tenant_id, assigned_user_id=mine)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    row = body["items"][0]
    assert row["assignee_name"] == "Mei Ling Tan"


async def test_an_unassigned_row_reports_a_null_assignee(client, tenant) -> None:
    tenant_id, mine = tenant
    await _opportunity(tenant_id, assigned_user_id=None)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    row = body["items"][0]
    assert row["assigned_user_id"] is None
    assert row["assignee_name"] is None


async def test_shared_with_me_is_true_for_a_named_share(client, tenant) -> None:
    tenant_id, mine = tenant
    owner = await _colleague(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=owner)
    await _share_with(tenant_id, opportunity_id, user_id=mine)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    row = body["items"][0]
    assert row["id"] == str(opportunity_id)
    assert row["shared_with_me"] is True


async def test_shared_with_me_is_true_for_a_tenant_broadcast(client, tenant) -> None:
    tenant_id, mine = tenant
    owner = await _colleague(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=owner)
    await _share_with(tenant_id, opportunity_id, tenant_scope=True)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    row = body["items"][0]
    assert row["id"] == str(opportunity_id)
    assert row["shared_with_me"] is True


async def test_the_assignee_join_never_crosses_a_tenant(client, tenant) -> None:
    """A job order's `assigned_user_id` is a bare uuid; only the tenant
    predicate stops the join from ever resolving a same-id user row that
    belongs to a different agency.

    RLS on `users` already blocks tenant B's row from being visible in a
    tenant-A session, so this does not by itself prove the join predicate
    is composite rather than bare on `id` alone -- both implementations
    pass under RLS. It stands as a regression guard: it fails loudly if
    RLS is ever weakened or bypassed (e.g. an admin-session code path)
    while the join stays on `id` alone.
    """
    tenant_a, mine = tenant
    tenant_b, other_user = await seed_tenant_with_user()
    try:
        async with AdminSessionLocal() as s:
            u = await s.get(User, other_user)
            u.preferred_name = "Not Me"
            await s.commit()
        await _opportunity(tenant_b, assigned_user_id=other_user)
        await _opportunity(tenant_a, assigned_user_id=None)

        sign_in(client, mine, tenant_a)
        body = (await client.get("/api/opportunities")).json()

        assert len(body["items"]) == 1
        row = body["items"][0]
        assert row["assigned_user_id"] is None
        assert row["assignee_name"] is None
        assert all(item.get("assignee_name") != "Not Me" for item in body["items"])
    finally:
        await cleanup_tenant(tenant_b)


async def test_shared_with_me_is_true_even_when_i_also_own_it(client, tenant) -> None:
    """The flag means "a share reaches you", not "only a share reaches you".

    A flag that flipped to false the moment you also owned the row would be
    describing something else.
    """
    tenant_id, mine = tenant
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    await _share_with(tenant_id, opportunity_id, user_id=mine)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities")).json()

    row = body["items"][0]
    assert row["id"] == str(opportunity_id)
    assert row["assigned_user_id"] == str(mine)
    assert row["shared_with_me"] is True
