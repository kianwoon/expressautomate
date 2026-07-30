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
from tests import test_opportunities_api as _api
from tests.conftest import AdminSessionLocal

# Re-used rather than re-written: these are the same two agencies and the same
# vacancy factory the list endpoint's own tests run against.
client = _api.client
seeded = _api.seeded
sign_in = _api.sign_in


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
