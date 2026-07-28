# allow-hardcode: "Parkway Shenton" / "Staff Nurse" / "Jane Tan" below are
# test fixture content specified verbatim by the task brief, not a
# matching/scoring oracle.
"""Roles a candidate held. Typed by a person; nothing here is AI-derived yet."""

import uuid

import pytest
from sqlalchemy import select, text

from app.db.rls import tenant_session
from app.models.candidate import CandidateRole
from tests.conftest import AdminSessionLocal


async def _a_candidate_row(tenant_id, user_id):
    candidate_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, email, "
                "pipeline_stage, record_status) "
                "VALUES (:i, :t, 'Jane Tan', :e, 'new', 'active')"
            ),
            {"i": candidate_id, "t": tenant_id, "e": f"jane{candidate_id.hex[:6]}@acme.sg"},
        )
        await s.commit()
    return candidate_id


@pytest.fixture
async def agency():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.commit()
    yield tid, uid
    async with AdminSessionLocal() as s:
        for table in (
            "candidate_roles",
            "candidate_field_overrides",
            "candidate_skills",
            "candidates",
            "users",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


@pytest.fixture
async def other_agency():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.commit()
    yield tid, uid
    async with AdminSessionLocal() as s:
        for table in (
            "candidate_roles",
            "candidate_field_overrides",
            "candidate_skills",
            "candidates",
            "users",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


@pytest.mark.asyncio
async def test_a_role_belongs_to_one_tenant_only(agency, other_agency):
    """Agency B cannot see Agency A's role even knowing its id.

    `agency` and `other_agency` each yield `(tenant_id, user_id)` and are
    defined in this module — see Task 3 Step 1 for the body. `tenant_session`
    is the real scoped session from `app.db.rls`, the same one the API uses.
    """
    a_tenant, a_user = agency
    b_tenant, _b_user = other_agency
    a_candidate = await _a_candidate_row(a_tenant, a_user)

    async with tenant_session(a_tenant) as session:
        session.add(
            CandidateRole(
                tenant_id=a_tenant,
                candidate_id=a_candidate,
                employer="Parkway Shenton",
                employer_normalized="parkway shenton",
                title="Staff Nurse",
                title_normalized="staff nurse",
                started_on=None,
                started_precision="month",
                source=CandidateRole.HUMAN,
                status=CandidateRole.CONFIRMED,
            )
        )
        await session.commit()

    async with tenant_session(b_tenant) as session:
        rows = (await session.execute(select(CandidateRole))).scalars().all()
        assert rows == []
