"""Who looks after a client, and what moves with them when that changes.

An account changing hands is not just a field edit: the work sitting under
that client belongs to whoever covers the account, so by default the job
orders travel with it and the response says how many did — a recruiter is
told "12 job orders moved to Sarah" rather than discovering it later.

"Open job orders" is not a thing this schema can express: `Opportunity`
carries `review_status` and `quality_state`, and neither says filled, closed
or lost. So every job order of that client currently assigned to the outgoing
recruiter moves, and no endpoint or string here says "open".
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from app.models.opportunity import Opportunity
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in


async def _http(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


@pytest.fixture
async def agency():
    """One agency, two recruiters, two clients.

    Two clients rather than one because the sharpest thing to prove is what
    does *not* move: a job order of the other client, assigned to the same
    outgoing recruiter, must be left where it is.
    """
    tid = uuid.uuid4()
    outgoing, incoming = uuid.uuid4(), uuid.uuid4()
    acme, other = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:8]}"},
        )
        for uid in (outgoing, incoming):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, role) "
                    "VALUES (:i, :t, :e, 'recruiter')"
                ),
                {"i": uid, "t": tid, "e": f"u{uid.hex[:8]}@agency.sg"},
            )
        for cid, name in ((acme, "Acme"), (other, "Bolt")):
            await s.execute(
                text(
                    "INSERT INTO clients (id, tenant_id, name, name_normalized, status, "
                    "assigned_user_id) VALUES (:i, :t, :n, :z, 'confirmed', :a)"
                ),
                {"i": cid, "t": tid, "n": name, "z": name.lower(), "a": outgoing},
            )
        await s.commit()
    yield tid, outgoing, incoming, acme, other
    await cleanup_tenant(tid)


async def _job_order(
    tid: uuid.UUID, client_id: uuid.UUID | None, assigned_user_id: uuid.UUID | None
) -> uuid.UUID:
    oid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        s.add(
            Opportunity(
                id=oid,
                tenant_id=tid,
                client_id=client_id,
                assigned_user_id=assigned_user_id,
            )
        )
        await s.commit()
    return oid


async def _assignee_of(opportunity_id: uuid.UUID) -> uuid.UUID | None:
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text("SELECT assigned_user_id FROM opportunities WHERE id = :i"),
                {"i": opportunity_id},
            )
        ).scalar_one()


async def test_assigning_a_client_returns_the_new_assignee(agency) -> None:
    tid, outgoing, incoming, acme, _other = agency
    async with await _http(tid, outgoing) as http:
        response = await http.put(
            f"/api/clients/{acme}/assignee", json={"user_id": str(incoming)}
        )
    assert response.status_code == 200
    assert response.json() == {
        "client_id": str(acme),
        "assigned_user_id": str(incoming),
        "opportunities_moved": 0,
    }
    async with AdminSessionLocal() as s:
        stored = (
            await s.execute(
                text("SELECT assigned_user_id FROM clients WHERE id = :i"), {"i": acme}
            )
        ).scalar_one()
    assert stored == incoming


async def test_the_work_travels_with_the_account_by_default(agency) -> None:
    """No `move_open_opportunities` in the body: the job orders still move."""
    tid, outgoing, incoming, acme, _other = agency
    first = await _job_order(tid, acme, outgoing)
    second = await _job_order(tid, acme, outgoing)

    async with await _http(tid, outgoing) as http:
        response = await http.put(
            f"/api/clients/{acme}/assignee", json={"user_id": str(incoming)}
        )

    assert response.status_code == 200
    assert response.json()["opportunities_moved"] == 2
    assert await _assignee_of(first) == incoming
    assert await _assignee_of(second) == incoming


async def test_a_client_nobody_was_looking_after_still_hands_over_its_work(agency) -> None:
    """The outgoing assignee may be NULL, and NULL is a value here.

    `= NULL` is never true in SQL, so an equality predicate would quietly move
    nothing in exactly this case — an unadopted client whose job orders are
    also unassigned. `IS NOT DISTINCT FROM` is what makes it work, and this is
    the only test that can tell the two apart.
    """
    tid, outgoing, incoming, acme, _other = agency
    async with AdminSessionLocal() as s:
        await s.execute(
            text("UPDATE clients SET assigned_user_id = NULL WHERE id = :i"), {"i": acme}
        )
        await s.commit()
    orphan = await _job_order(tid, acme, None)
    # Somebody else's job order for the same client must not be swept up.
    theirs = await _job_order(tid, acme, outgoing)

    async with await _http(tid, outgoing) as http:
        response = await http.put(
            f"/api/clients/{acme}/assignee", json={"user_id": str(incoming)}
        )

    assert response.json()["opportunities_moved"] == 1
    assert await _assignee_of(orphan) == incoming
    assert await _assignee_of(theirs) == outgoing


async def test_saying_no_moves_nothing(agency) -> None:
    tid, outgoing, incoming, acme, _other = agency
    mid_placement = await _job_order(tid, acme, outgoing)

    async with await _http(tid, outgoing) as http:
        response = await http.put(
            f"/api/clients/{acme}/assignee",
            json={"user_id": str(incoming), "move_open_opportunities": False},
        )

    assert response.json()["opportunities_moved"] == 0
    assert await _assignee_of(mid_placement) == outgoing


async def test_another_clients_job_order_is_untouched(agency) -> None:
    """Same outgoing recruiter, different client: it stays put."""
    tid, outgoing, incoming, acme, other = agency
    mine = await _job_order(tid, acme, outgoing)
    theirs = await _job_order(tid, other, outgoing)

    async with await _http(tid, outgoing) as http:
        response = await http.put(
            f"/api/clients/{acme}/assignee", json={"user_id": str(incoming)}
        )

    assert response.json()["opportunities_moved"] == 1
    assert await _assignee_of(mine) == incoming
    assert await _assignee_of(theirs) == outgoing


async def test_a_client_can_be_left_unassigned(agency) -> None:
    tid, outgoing, _incoming, acme, _other = agency
    job_order = await _job_order(tid, acme, outgoing)

    async with await _http(tid, outgoing) as http:
        response = await http.put(f"/api/clients/{acme}/assignee", json={"user_id": None})

    assert response.status_code == 200
    assert response.json()["assigned_user_id"] is None
    assert await _assignee_of(job_order) is None


async def test_assigning_a_client_that_does_not_exist_is_a_404(agency) -> None:
    tid, outgoing, incoming, _acme, _other = agency
    async with await _http(tid, outgoing) as http:
        response = await http.put(
            f"/api/clients/{uuid.uuid4()}/assignee", json={"user_id": str(incoming)}
        )
    assert response.status_code == 404


async def test_another_agencys_client_cannot_be_reassigned(agency) -> None:
    """RLS hides the row, so it reads as absent rather than forbidden."""
    tid, outgoing, incoming, acme, _other = agency
    stranger_tenant, stranger = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": stranger_tenant, "n": f"agency-{stranger_tenant.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'recruiter')"
            ),
            {"i": stranger, "t": stranger_tenant, "e": f"u{stranger.hex[:8]}@other.sg"},
        )
        await s.commit()
    try:
        async with await _http(stranger_tenant, stranger) as http:
            response = await http.put(
                f"/api/clients/{acme}/assignee", json={"user_id": str(incoming)}
            )
        assert response.status_code == 404
        async with AdminSessionLocal() as s:
            still = (
                await s.execute(
                    text("SELECT assigned_user_id FROM clients WHERE id = :i"), {"i": acme}
                )
            ).scalar_one()
        assert still == outgoing
    finally:
        await cleanup_tenant(stranger_tenant)
    _ = tid


async def test_an_anonymous_caller_cannot_reassign_a_client(agency) -> None:
    _tid, _outgoing, incoming, acme, _other = agency
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.put(
            f"/api/clients/{acme}/assignee", json={"user_id": str(incoming)}
        )
    assert response.status_code == 401


async def _collaborators_of(client_id: uuid.UUID) -> list[uuid.UUID]:
    async with AdminSessionLocal() as s:
        return list(
            (
                await s.execute(
                    text("SELECT user_id FROM client_collaborators WHERE client_id = :i"),
                    {"i": client_id},
                )
            ).scalars()
        )


async def test_adding_the_same_collaborator_twice_is_idempotent(agency) -> None:
    tid, outgoing, incoming, acme, _other = agency
    async with await _http(tid, outgoing) as http:
        first = await http.post(
            f"/api/clients/{acme}/collaborators", json={"user_id": str(incoming)}
        )
        second = await http.post(
            f"/api/clients/{acme}/collaborators", json={"user_id": str(incoming)}
        )

    assert first.status_code == 201
    # The second call is a no-op, not a 500 from `uq_client_collaborators_once`.
    assert second.status_code == 201
    assert second.json() == first.json()
    assert await _collaborators_of(acme) == [incoming]


async def test_a_collaborator_can_be_removed(agency) -> None:
    tid, outgoing, incoming, acme, _other = agency
    async with await _http(tid, outgoing) as http:
        await http.post(f"/api/clients/{acme}/collaborators", json={"user_id": str(incoming)})
        removed = await http.delete(f"/api/clients/{acme}/collaborators/{incoming}")
        # Removing one that is already gone is the same no-op as adding twice.
        again = await http.delete(f"/api/clients/{acme}/collaborators/{incoming}")

    assert removed.status_code == 204
    assert again.status_code == 204
    assert await _collaborators_of(acme) == []


async def test_a_collaborator_on_a_client_that_does_not_exist_is_a_404(agency) -> None:
    tid, outgoing, incoming, _acme, _other = agency
    async with await _http(tid, outgoing) as http:
        response = await http.post(
            f"/api/clients/{uuid.uuid4()}/collaborators", json={"user_id": str(incoming)}
        )
    assert response.status_code == 404


async def test_a_collaborator_grants_no_sight_of_the_clients_work(agency) -> None:
    """Cover is a record, not a share (see `ClientCollaborator`).

    Named here so that anyone who later makes it an implicit grant has to
    delete a test that says why it is not one.
    """
    tid, outgoing, incoming, acme, _other = agency
    job_order = await _job_order(tid, acme, outgoing)
    async with await _http(tid, outgoing) as http:
        await http.post(f"/api/clients/{acme}/collaborators", json={"user_id": str(incoming)})
    async with await _http(tid, incoming) as http:
        listed = (await http.get("/api/opportunities")).json()
    assert [row["id"] for row in listed["items"]] == []
    assert await _assignee_of(job_order) == outgoing


async def test_an_anonymous_caller_cannot_add_a_collaborator(agency) -> None:
    _tid, _outgoing, incoming, acme, _other = agency
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(
            f"/api/clients/{acme}/collaborators", json={"user_id": str(incoming)}
        )
    assert response.status_code == 401
