"""Agency A must never reach agency B's clients — including by foreign key.

The FK case is the one RLS does not cover. A policy filters what a statement
can SELECT and what it may INSERT, but PostgreSQL validates a foreign key
with an internal referential-integrity check that is not subject to the
policy. So a mention row in agency A can name agency B's client_id and the
database will happily accept it, silently stitching one agency's evidence
onto another's record. Only a composite FK carrying tenant_id closes it.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session


async def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    from tests.conftest import AdminSessionLocal

    async with AdminSessionLocal() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
            {"i": tenant_id, "n": slug, "s": slug},
        )
        await session.commit()


@pytest.fixture
async def two_agencies():
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(a, f"agency-a-{a.hex[:6]}")
    await _seed_tenant(b, f"agency-b-{b.hex[:6]}")
    yield a, b
    from tests.conftest import AdminSessionLocal

    async with AdminSessionLocal() as session:
        for tid in (a, b):
            await session.execute(
                text("DELETE FROM client_mentions WHERE tenant_id = :t"), {"t": tid}
            )
            await session.execute(text("DELETE FROM clients WHERE tenant_id = :t"), {"t": tid})
            await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await session.commit()


async def test_one_agency_cannot_read_anothers_clients(two_agencies) -> None:
    a, b = two_agencies
    async with tenant_session(a) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                "VALUES (:i, :t, 'Acme', 'acme', 'unconfirmed')"
            ),
            {"i": uuid.uuid4(), "t": a},
        )
        await session.commit()

    async with tenant_session(b) as session:
        rows = (await session.execute(text("SELECT id FROM clients"))).all()
    assert rows == []


async def test_a_mention_cannot_reference_another_agencys_client(two_agencies) -> None:
    a, b = two_agencies
    client_id = uuid.uuid4()
    async with tenant_session(a) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                "VALUES (:i, :t, 'Acme', 'acme', 'unconfirmed')"
            ),
            {"i": client_id, "t": a},
        )
        await session.commit()

    # Agency B names agency A's client. The composite FK must reject it.
    with pytest.raises(IntegrityError):
        async with tenant_session(b) as session:
            await session.execute(
                text(
                    "INSERT INTO client_mentions "
                    "(id, tenant_id, client_id, matched_by) "
                    "VALUES (:i, :t, :c, 'human')"
                ),
                {"i": uuid.uuid4(), "t": b, "c": client_id},
            )
            await session.commit()
