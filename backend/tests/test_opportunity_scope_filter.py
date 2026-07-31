"""Ownership is a second axis, independent of review status."""

import uuid

import pytest

from app.models import Opportunity, User
from app.models.opportunity_share import OpportunityShare
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user, sign_in


async def _opportunity(
    tenant_id: uuid.UUID, *, assigned_user_id: uuid.UUID | None = None
) -> uuid.UUID:
    """A job order with no email behind it — typed in by hand."""
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


async def _colleague(tenant_id: uuid.UUID) -> uuid.UUID:
    other = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(User(id=other, tenant_id=tenant_id, email=f"{other.hex[:8]}@agency.sg"))
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


async def test_scope_mine_returns_only_my_job_orders(client, tenant) -> None:
    tenant_id, mine = tenant
    mine_id = await _opportunity(tenant_id, assigned_user_id=mine)
    await _opportunity(tenant_id, assigned_user_id=None)  # queue

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities", params={"scope": "mine"})).json()

    assert [item["id"] for item in body["items"]] == [str(mine_id)]


async def test_scope_queue_returns_only_unassigned_ones(client, tenant) -> None:
    tenant_id, mine = tenant
    await _opportunity(tenant_id, assigned_user_id=mine)
    queue_id = await _opportunity(tenant_id, assigned_user_id=None)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities", params={"scope": "queue"})).json()

    assert [item["id"] for item in body["items"]] == [str(queue_id)]


async def test_scope_shared_with_me_returns_shares_and_broadcasts(client, tenant) -> None:
    tenant_id, mine = tenant
    owner = await _colleague(tenant_id)

    named_share_id = await _opportunity(tenant_id, assigned_user_id=owner)
    await _share_with(tenant_id, named_share_id, user_id=mine)

    broadcast_id = await _opportunity(tenant_id, assigned_user_id=owner)
    await _share_with(tenant_id, broadcast_id, tenant_scope=True)

    # Not shared at all, and owned by someone else — must not appear.
    await _opportunity(tenant_id, assigned_user_id=owner)
    # Mine, but never shared — must not appear either: this scope is about
    # shares, not ownership.
    await _opportunity(tenant_id, assigned_user_id=mine)

    sign_in(client, mine, tenant_id)
    body = (
        await client.get("/api/opportunities", params={"scope": "shared_with_me"})
    ).json()

    assert {item["id"] for item in body["items"]} == {
        str(named_share_id),
        str(broadcast_id),
    }


async def test_scope_all_is_the_default_and_matches_the_predicate(client, tenant) -> None:
    """`all` means everything the caller may see, which is not everything the
    agency has."""
    tenant_id, mine = tenant
    owner = await _colleague(tenant_id)
    visible_mine = await _opportunity(tenant_id, assigned_user_id=mine)
    visible_queue = await _opportunity(tenant_id, assigned_user_id=None)
    # Assigned to a colleague, never shared: outside the predicate entirely.
    await _opportunity(tenant_id, assigned_user_id=owner)

    sign_in(client, mine, tenant_id)
    default_body = (await client.get("/api/opportunities")).json()
    explicit_body = (
        await client.get("/api/opportunities", params={"scope": "all"})
    ).json()

    expected = {str(visible_mine), str(visible_queue)}
    assert {item["id"] for item in default_body["items"]} == expected
    assert {item["id"] for item in explicit_body["items"]} == expected


async def test_no_scope_can_widen_visibility(client, tenant) -> None:
    """Each scope is a filter WITHIN what the predicate allows. A colleague's
    private job order appears under none of the four.

    Only the `all` leg is load-bearing against a dropped predicate. Mutating
    `app/api/opportunities.py` to read `.where(scope_clause)` instead of
    `.where(visible).where(scope_clause)` (both call sites) makes only the
    `all` iteration fail here; `mine`, `queue` and `shared_with_me` still
    pass, and not by coincidence of this fixture — by construction of
    `_scope_clause` (app/api/opportunities.py) against `visible_opportunities`
    (app/services/visibility.py):

    - `mine`'s clause is `assigned_user_id == user_id`, which is verbatim one
      of the `or_(...)` branches in `visible_opportunities`. Any row the
      clause admits, the predicate admits too — there is no row that can
      satisfy `mine` and be hidden by the predicate, so this leg cannot
      discriminate a dropped predicate from an intact one.
    - `queue`'s clause is `assigned_user_id.is_(None)`, also verbatim one of
      the predicate's `or_(...)` branches. Same reasoning, same conclusion.
    - `shared_with_me`'s clause is `_shared_with_me_exists(user_id)`, the
      exact function `visible_opportunities` uses for its own
      `shared_with_me` branch — not merely similar, the identical query. A
      row satisfying the clause always satisfies the predicate.

    So `private_id` here (assigned to a colleague, never shared) is excluded
    from `mine`/`queue`/`shared_with_me` by the scope clause alone, before
    the predicate ever gets a say — those three legs read as coverage against
    a dropped predicate but carry none. `all`'s clause is `true_()`, which is
    NOT a predicate sub-branch, so it is the only leg where dropping the
    predicate can be observed: `true_()` alone would admit `private_id`,
    which is exactly what the mutation above demonstrates.
    """
    tenant_id, mine = tenant
    owner = await _colleague(tenant_id)
    private_id = await _opportunity(tenant_id, assigned_user_id=owner)

    sign_in(client, mine, tenant_id)
    for scope in ("mine", "queue", "shared_with_me", "all"):
        body = (
            await client.get("/api/opportunities", params={"scope": scope})
        ).json()
        assert str(private_id) not in {item["id"] for item in body["items"]}, scope


async def test_the_counts_follow_the_scope(client, tenant) -> None:
    """A count that ignored the scope would say twelve and then show four."""
    tenant_id, mine = tenant
    await _opportunity(tenant_id, assigned_user_id=mine)
    await _opportunity(tenant_id, assigned_user_id=mine)
    await _opportunity(tenant_id, assigned_user_id=None)

    sign_in(client, mine, tenant_id)
    body = (await client.get("/api/opportunities", params={"scope": "mine"})).json()

    assert len(body["items"]) == 2
    assert body["total"] == 2


async def test_an_unknown_scope_is_refused(client, tenant) -> None:
    tenant_id, mine = tenant
    sign_in(client, mine, tenant_id)

    response = await client.get(
        "/api/opportunities", params={"scope": "everything"}
    )

    assert response.status_code == 422
