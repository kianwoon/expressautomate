"""Agency A must never reach agency B's candidates — including by foreign key.

RLS filters what a statement may SELECT and INSERT. It does not filter the
internal referential-integrity check behind a foreign key, so a skill row in
agency A can name agency B's candidate and Postgres accepts it, silently
attaching one agency's data to another's person. Only a composite FK carrying
tenant_id closes that.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from tests.conftest import AdminSessionLocal

_INSERT = (
    "INSERT INTO candidates (id, tenant_id, full_name, email, record_status, "
    "pipeline_stage) VALUES (:i, :t, 'Jane Tan', :e, 'active', 'new')"
)


@pytest.fixture
async def two_agencies():
    a, b = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        for tid in (a, b):
            await s.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
                {"i": tid, "n": f"agency-{tid.hex[:6]}"},
            )
        await s.commit()
    yield a, b
    async with AdminSessionLocal() as s:
        for tid in (a, b):
            for table in (
                "candidate_field_overrides",
                "candidate_skills",
                "candidates",
                "users",
            ):
                await s.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid}
                )
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def test_one_agency_cannot_read_anothers_candidates(two_agencies) -> None:
    a, b = two_agencies
    async with tenant_session(a) as s:
        await s.execute(text(_INSERT), {"i": uuid.uuid4(), "t": a, "e": "jane@acme.sg"})
        await s.commit()
    async with tenant_session(b) as s:
        assert (await s.execute(text("SELECT id FROM candidates"))).all() == []


async def test_a_skill_cannot_reference_another_agencys_candidate(two_agencies) -> None:
    a, b = two_agencies
    cid = uuid.uuid4()
    async with tenant_session(a) as s:
        await s.execute(text(_INSERT), {"i": cid, "t": a, "e": "jane@acme.sg"})
        await s.commit()

    with pytest.raises(IntegrityError):
        async with tenant_session(b) as s:
            await s.execute(
                text(
                    "INSERT INTO candidate_skills "
                    "(id, tenant_id, candidate_id, skill, skill_normalized) "
                    "VALUES (:i, :t, :c, 'Python', 'python')"
                ),
                {"i": uuid.uuid4(), "t": b, "c": cid},
            )
            await s.commit()


async def test_an_override_cannot_reference_another_agencys_candidate(two_agencies) -> None:
    a, b = two_agencies
    cid = uuid.uuid4()
    async with tenant_session(a) as s:
        await s.execute(text(_INSERT), {"i": cid, "t": a, "e": "jane@acme.sg"})
        await s.commit()

    with pytest.raises(IntegrityError):
        async with tenant_session(b) as s:
            await s.execute(
                text(
                    "INSERT INTO candidate_field_overrides "
                    "(id, tenant_id, candidate_id, field_name, human_value) "
                    "VALUES (:i, :t, :c, 'full_name', 'Someone Else')"
                ),
                {"i": uuid.uuid4(), "t": b, "c": cid},
            )
            await s.commit()
