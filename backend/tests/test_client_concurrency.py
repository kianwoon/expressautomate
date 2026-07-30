"""Two workers, one new domain, one client.

The mailbox sync fans out across messages, so two extractions from the same
company can reach the matcher within microseconds of each other. Both will
find no existing client and both will insert. Without the `ON CONFLICT` on the
partial domain index the loser raises a unique violation, which fails an
extraction over a race that has an obvious correct answer.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.client_matching import match_client
from tests.conftest import AdminSessionLocal, cleanup_tenant


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


async def _match_once(tenant_id: uuid.UUID, sender: str) -> uuid.UUID | None:
    async with tenant_session(tenant_id) as session:
        matched = await match_client(session, tenant_id, None, sender, "Acme Pte Ltd")
        await session.commit()
        return matched.client_id if matched else None


async def test_two_concurrent_matches_produce_one_client(agency) -> None:
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
