"""Two workers, one new domain, one client.

The mailbox sync fans out across messages, so two extractions from the same
company can reach the matcher within microseconds of each other. Both will
find no existing client and both will insert. Without the `ON CONFLICT` on the
partial domain index the loser raises a unique violation, which fails an
extraction over a race that has an obvious correct answer.
"""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.main import app
from app.services.client_matching import match_client
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in


@pytest.fixture
async def agency():
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    yield tid
    await cleanup_tenant(tid)


async def _match_once(tenant_id: uuid.UUID, sender: str, company: str | None = None) -> uuid.UUID | None:
    async with tenant_session(tenant_id) as session:
        matched = await match_client(session, tenant_id, None, sender, company)
        await session.commit()
        return matched.client_id if matched else None


async def test_two_concurrent_domain_matches_produce_one_client(agency) -> None:
    """The partial unique index on (tenant_id, email_domain) deduplicates
    concurrent inserts: two workers hitting the same domain resolve to one row
    via the ON CONFLICT clause. Name-based matching has no unique index (by
    design — two firms can share a name) and tolerates duplicates a recruiter
    merges later, so this guarantee is domain-specific."""
    results = await asyncio.gather(
        _match_once(agency, "hr@acme.com.sg"),
        _match_once(agency, "jobs@acme.com.sg"),
        return_exceptions=True,
    )
    for r in results:
        assert not isinstance(r, Exception), f"concurrent match raised: {r!r}"

    async with tenant_session(agency) as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM clients WHERE email_domain = 'acme.com.sg'")
            )
        ).scalar_one()
    assert count == 1


"""The reassignment race, below.

`PUT /clients/{id}/assignee` decides permission from the client's current
assignee and then, in the same breath, moves that recruiter's entire book of
work under the client. Read and write must therefore be serialised against a
concurrent reassignment, which is what `FOR UPDATE` on that read buys.
"""


async def _reassignment_race(agency_id: uuid.UUID) -> int:
    """Drive the race and return the status code the losing request gets.

    A held transaction stands in for the concurrent request: it locks the
    client row and reassigns it away from `holder`, then commits only after
    the API request is already in flight. With `FOR UPDATE` the API blocks on
    that lock and, once released, reads the *committed* assignee — no longer
    `holder`, so 403. Without it the API reads its own older snapshot, sees
    `holder`, and proceeds. The sleep is not a race window: it only guarantees
    the request is in flight before the commit, so a slow machine makes the
    test slower, never flakier.
    """
    holder, newcomer = uuid.uuid4(), uuid.uuid4()
    client_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        for uid in (holder, newcomer):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, role) "
                    "VALUES (:i, :t, :e, 'recruiter')"
                ),
                {"i": uid, "t": agency_id, "e": f"u{uid.hex[:8]}@agency.sg"},
            )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status, "
                "assigned_user_id) VALUES (:i, :t, 'Acme', 'acme', 'confirmed', :a)"
            ),
            {"i": client_id, "t": agency_id, "a": holder},
        )
        await s.commit()

    async def _hold_then_commit() -> None:
        async with AdminSessionLocal() as s:
            await s.execute(
                text("SELECT id FROM clients WHERE id = :i FOR UPDATE"), {"i": client_id}
            )
            await s.execute(
                text("UPDATE clients SET assigned_user_id = :u WHERE id = :i"),
                {"u": newcomer, "i": client_id},
            )
            await asyncio.sleep(1.0)
            await s.commit()

    async def _reassign() -> int:
        # Started after the lock is taken, so the ordering is fixed.
        await asyncio.sleep(0.2)
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        sign_in(c, holder, agency_id)
        async with c as http:
            response = await http.put(
                f"/api/clients/{client_id}/assignee", json={"user_id": str(holder)}
            )
        return response.status_code

    _held, status = await asyncio.gather(_hold_then_commit(), _reassign())
    return status


async def test_a_stale_assignee_cannot_reassign_under_a_concurrent_handover(
    agency,
) -> None:
    assert await _reassignment_race(agency) == 403
