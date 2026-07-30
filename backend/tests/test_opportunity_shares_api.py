"""Passing a job order to whoever can fill it.

The rule under test is narrow and easy to get wrong in either direction: a
share is sight and nothing more, sight is itself the right to pass it on, and
throwing the job order open to the whole agency is the assignee's call alone.
"""

import uuid

import pytest

from tests import test_opportunities_api as _api
from tests.test_opportunity_visibility_routes import _colleague

client = _api.client
seeded = _api.seeded
sign_in = _api.sign_in


@pytest.fixture
async def assigned(seeded):
    """One agency, one job order, assigned to `owner_of_the_work`."""
    make_tenant, make_opportunity, _make_evidence = seeded
    tenant_id, assignee, mailbox_id = await make_tenant("agency-a")
    opportunity_id = await make_opportunity(
        tenant_id, mailbox_id, company_name_raw="Acme Pte Ltd", assigned_user_id=assignee
    )
    return tenant_id, assignee, opportunity_id


async def _listed(client) -> list[str]:
    body = (await client.get("/api/opportunities")).json()
    return [item["id"] for item in body["items"]]


async def test_a_named_colleague_gains_sight_but_not_the_pen(client, assigned) -> None:
    tenant_id, assignee, opportunity_id = assigned
    colleague = await _colleague(tenant_id)

    sign_in(client, assignee, tenant_id)
    shared = await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "user", "user_ids": [str(colleague)], "note": "you know Acme"},
    )
    assert shared.status_code == 200, shared.text
    assert shared.json()["newly_shared_with"] == 1

    sign_in(client, colleague, tenant_id)
    assert await _listed(client) == [str(opportunity_id)]

    # Sight is not the pen.
    refused = await client.post(
        f"/api/opportunities/{opportunity_id}/review", json={"reviewed": True}
    )
    assert refused.status_code == 403, refused.text


async def test_resharing_updates_the_note_rather_than_conflicting(
    client, assigned
) -> None:
    tenant_id, assignee, opportunity_id = assigned
    colleague = await _colleague(tenant_id)
    sign_in(client, assignee, tenant_id)

    first = await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "user", "user_ids": [str(colleague)], "note": "first"},
    )
    assert first.status_code == 200, first.text

    again = await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "user", "user_ids": [str(colleague)], "note": "second"},
    )
    assert again.status_code == 200, again.text
    # Already told once; a second message would say nothing new.
    assert again.json()["newly_shared_with"] == 0

    shares = (await client.get(f"/api/opportunities/{opportunity_id}/shares")).json()
    rows = [r for r in shares["items"] if r["shared_with_user_id"] == str(colleague)]
    assert len(rows) == 1, rows
    assert rows[0]["note"] == "second"


async def test_a_recipient_may_pass_it_on_to_a_third_recruiter(client, assigned) -> None:
    """The chain: seeing it is what entitles you to share it onward."""
    tenant_id, assignee, opportunity_id = assigned
    colleague = await _colleague(tenant_id)
    third = await _colleague(tenant_id)

    sign_in(client, assignee, tenant_id)
    await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "user", "user_ids": [str(colleague)]},
    )

    sign_in(client, colleague, tenant_id)
    onward = await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "user", "user_ids": [str(third)], "note": "you're the Acme one"},
    )
    assert onward.status_code == 200, onward.text

    sign_in(client, third, tenant_id)
    assert await _listed(client) == [str(opportunity_id)]


async def test_a_recipient_may_not_broadcast_to_the_whole_agency(
    client, assigned
) -> None:
    """Someone else's client work is not theirs to put on the open market."""
    tenant_id, assignee, opportunity_id = assigned
    colleague = await _colleague(tenant_id)

    sign_in(client, assignee, tenant_id)
    await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "user", "user_ids": [str(colleague)]},
    )

    sign_in(client, colleague, tenant_id)
    refused = await client.post(
        f"/api/opportunities/{opportunity_id}/shares", json={"scope": "tenant"}
    )
    assert refused.status_code == 403, refused.text
    assert "whole agency" in refused.json()["detail"]


async def test_the_assignee_may_broadcast_and_a_later_hire_inherits_it(
    client, assigned
) -> None:
    """One row, not one per colleague — which is what lets a recruiter hired
    next month see it without anyone re-sharing."""
    tenant_id, assignee, opportunity_id = assigned

    sign_in(client, assignee, tenant_id)
    broadcast = await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "tenant", "note": "anyone with a Java bench"},
    )
    assert broadcast.status_code == 201, broadcast.text

    hired_afterwards = await _colleague(tenant_id)
    sign_in(client, hired_afterwards, tenant_id)
    assert await _listed(client) == [str(opportunity_id)]


async def test_a_recipient_removes_themselves_but_a_stranger_cannot(
    client, assigned
) -> None:
    tenant_id, assignee, opportunity_id = assigned
    colleague = await _colleague(tenant_id)
    stranger = await _colleague(tenant_id)

    sign_in(client, assignee, tenant_id)
    await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "user", "user_ids": [str(colleague)]},
    )
    share_id = (await client.get(f"/api/opportunities/{opportunity_id}/shares")).json()[
        "items"
    ][0]["id"]

    # A stranger cannot even see the job order, so the share is a 404 — a 403
    # would confirm the row exists.
    sign_in(client, stranger, tenant_id)
    refused = await client.delete(
        f"/api/opportunities/{opportunity_id}/shares/{share_id}"
    )
    assert refused.status_code == 404, refused.text

    sign_in(client, colleague, tenant_id)
    removed = await client.delete(
        f"/api/opportunities/{opportunity_id}/shares/{share_id}"
    )
    assert removed.status_code == 204, removed.text
    assert await _listed(client) == []


async def test_sharing_a_job_order_you_cannot_see_is_a_404(client, assigned) -> None:
    tenant_id, _assignee, opportunity_id = assigned
    stranger = await _colleague(tenant_id)
    sign_in(client, stranger, tenant_id)

    response = await client.post(
        f"/api/opportunities/{opportunity_id}/shares",
        json={"scope": "user", "user_ids": [str(uuid.uuid4())]},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "No such job order."
