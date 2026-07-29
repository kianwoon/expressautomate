"""Who is even a candidate for this job order.

Database-backed on purpose: the whole rule is a query, and the tenant boundary
it runs behind is exactly the part a pure test could not check.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.sourcing.eligible import eligible_candidates
from tests.conftest import AdminSessionLocal, cleanup_tenant

_INSERT_CANDIDATE = text(
    "INSERT INTO candidates (id, tenant_id, full_name, record_status, pipeline_stage) "
    "VALUES (:i, :t, :n, :r, :p)"
)
_INSERT_CLIENT = text(
    "INSERT INTO clients (id, tenant_id, name, name_normalized) VALUES (:i, :t, :n, :n)"
)
_INSERT_SUBMISSION = text(
    "INSERT INTO candidate_submissions (id, tenant_id, candidate_id, client_id) "
    "VALUES (:i, :t, :c, :cl)"
)


async def _tenant() -> uuid.UUID:
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    return tid


@pytest.fixture
async def agency():
    tid = await _tenant()
    yield tid
    await cleanup_tenant(tid)


@pytest.fixture
async def other_agency():
    tid = await _tenant()
    yield tid
    await cleanup_tenant(tid)


async def _client(tenant_id: uuid.UUID, name: str = "Acme Health") -> uuid.UUID:
    cid = uuid.uuid4()
    async with tenant_session(tenant_id) as s:
        await s.execute(_INSERT_CLIENT, {"i": cid, "t": tenant_id, "n": name})
        await s.commit()
    return cid


async def _candidate(
    tenant_id: uuid.UUID,
    *,
    name: str = "Jane Tan",
    record_status: str = "active",
    pipeline_stage: str = "new",
) -> uuid.UUID:
    cid = uuid.uuid4()
    async with tenant_session(tenant_id) as s:
        await s.execute(
            _INSERT_CANDIDATE,
            {
                "i": cid,
                "t": tenant_id,
                "n": name,
                "r": record_status,
                "p": pipeline_stage,
            },
        )
        await s.commit()
    return cid


async def _eligible(tenant_id: uuid.UUID, client_id: uuid.UUID) -> list[uuid.UUID]:
    async with tenant_session(tenant_id) as s:
        return await eligible_candidates(s, tenant_id=tenant_id, client_id=client_id)


async def test_only_active_records_are_eligible(agency) -> None:
    client_id = await _client(agency)
    live = await _candidate(agency)
    await _candidate(agency, name="Archived Ann", record_status="archived")

    assert await _eligible(agency, client_id) == [live]


async def test_a_placed_candidate_is_not_eligible(agency) -> None:
    client_id = await _client(agency)
    live = await _candidate(agency)
    await _candidate(agency, name="Placed Pat", pipeline_stage="placed")

    assert await _eligible(agency, client_id) == [live]


async def test_a_rejected_candidate_is_still_eligible(agency) -> None:
    """The rejection was against one role and says nothing about this one."""
    client_id = await _client(agency)
    rejected = await _candidate(agency, name="Rejected Raj", pipeline_stage="rejected")

    assert await _eligible(agency, client_id) == [rejected]


async def test_a_submitted_candidate_disappears_and_returns_when_undone(agency) -> None:
    client_id = await _client(agency)
    candidate_id = await _candidate(agency)
    submission_id = uuid.uuid4()

    assert await _eligible(agency, client_id) == [candidate_id]

    async with tenant_session(agency) as s:
        await s.execute(
            _INSERT_SUBMISSION,
            {"i": submission_id, "t": agency, "c": candidate_id, "cl": client_id},
        )
        await s.commit()

    assert await _eligible(agency, client_id) == []

    async with tenant_session(agency) as s:
        await s.execute(
            text("DELETE FROM candidate_submissions WHERE id = :i"), {"i": submission_id}
        )
        await s.commit()

    assert await _eligible(agency, client_id) == [candidate_id]


async def test_a_submission_to_one_client_does_not_hide_the_candidate_elsewhere(
    agency,
) -> None:
    seen = await _client(agency, "Acme Health")
    unseen = await _client(agency, "Beta Clinic")
    candidate_id = await _candidate(agency)

    async with tenant_session(agency) as s:
        await s.execute(
            _INSERT_SUBMISSION,
            {"i": uuid.uuid4(), "t": agency, "c": candidate_id, "cl": seen},
        )
        await s.commit()

    assert await _eligible(agency, seen) == []
    assert await _eligible(agency, unseen) == [candidate_id]


async def test_another_agencys_candidates_are_unreachable(agency, other_agency) -> None:
    client_id = await _client(agency)
    mine = await _candidate(agency, name="Mine")
    await _candidate(other_agency, name="Theirs")

    assert await _eligible(agency, client_id) == [mine]
