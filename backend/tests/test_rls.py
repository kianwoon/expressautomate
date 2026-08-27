"""Row-level security: prove Agency A cannot read Agency B (plan §18).

These tests are the reason RLS was added before any business table exists. They
run as the restricted runtime role, so a regression in the policy — or a
DATABASE_URL quietly repointed at the admin role — fails here rather than in
production.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.db.rls import tenant_session, verify_rls_enforced
from app.db.session import SessionLocal

# The two-agencies fixture clears EVERY user row (DELETE FROM users) to prove
# isolation from an empty slate; run concurrently that collides with any other
# file's writes. Same global-state class f48cc82 serializes — run serially.
pytestmark = pytest.mark.serial


async def _seed(tenant_id: uuid.UUID, slug: str, email: str) -> None:
    """Seed a tenant through its own scope — the policy permits exactly this."""
    async with tenant_session(tenant_id) as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
            {"i": tenant_id, "n": slug, "s": slug},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'recruiter')"),
            {"i": uuid.uuid4(), "t": tenant_id, "e": email},
        )


@pytest.fixture
async def two_agencies() -> tuple[uuid.UUID, uuid.UUID]:
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed(a, f"agency-a-{a.hex[:6]}", "a@agency-a.sg")
    await _seed(b, f"agency-b-{b.hex[:6]}", "b@agency-b.sg")
    yield a, b
    for tid in (a, b):
        async with tenant_session(tid) as s:
            await s.execute(text("DELETE FROM users"))
            await s.execute(text("DELETE FROM tenants"))


async def test_runtime_role_cannot_bypass_rls() -> None:
    """If this fails, every other isolation test is meaningless."""
    async with SessionLocal() as s:
        bypasses = (
            await s.execute(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"))
        ).scalar_one()
    assert bypasses is False, "runtime role must not have BYPASSRLS"


async def test_startup_check_passes() -> None:
    await verify_rls_enforced()


async def test_tenant_sees_only_its_own_users(two_agencies) -> None:
    a, b = two_agencies
    async with tenant_session(a) as s:
        emails = (await s.execute(text("SELECT email FROM users"))).scalars().all()
    assert emails == ["a@agency-a.sg"]

    async with tenant_session(b) as s:
        emails = (await s.execute(text("SELECT email FROM users"))).scalars().all()
    assert emails == ["b@agency-b.sg"]


async def test_unscoped_session_sees_nothing(two_agencies) -> None:
    """Fail closed: forgetting to scope yields an empty result, not a leak."""
    async with SessionLocal() as s:
        rows = (await s.execute(text("SELECT count(*) FROM users"))).scalar_one()
    assert rows == 0


async def test_cannot_write_a_row_belonging_to_another_tenant(two_agencies) -> None:
    """WITH CHECK stops a scoped session forging another agency's tenant_id."""
    a, b = two_agencies
    with pytest.raises(DBAPIError):
        async with tenant_session(a) as s:
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, role) "
                    "VALUES (:i, :t, :e, 'recruiter')"
                ),
                {"i": uuid.uuid4(), "t": b, "e": "smuggled@agency-a.sg"},
            )


async def test_cannot_update_another_tenants_row(two_agencies) -> None:
    a, b = two_agencies
    async with tenant_session(a) as s:
        result = await s.execute(
            text("UPDATE users SET display_name = 'hacked' WHERE tenant_id = :t"),
            {"t": b},
        )
        assert result.rowcount == 0

    async with tenant_session(b) as s:
        names = (await s.execute(text("SELECT display_name FROM users"))).scalars().all()
    assert names == [None]


async def test_setting_does_not_leak_between_sessions(two_agencies) -> None:
    """SET LOCAL must die with its transaction, or a pooled connection leaks."""
    a, _ = two_agencies
    async with tenant_session(a) as s:
        assert (await s.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1

    async with SessionLocal() as s:
        setting = (
            await s.execute(text("SELECT current_setting('app.tenant_id', true)"))
        ).scalar_one()
    assert setting in (None, ""), f"tenant setting leaked: {setting!r}"


async def test_app_role_cannot_read_migration_history() -> None:
    """Schema version is the admin role's business."""
    with pytest.raises(DBAPIError):
        async with SessionLocal() as s:
            await s.execute(text("SELECT * FROM alembic_version"))


def test_database_url_uses_the_restricted_role() -> None:
    assert settings.DATABASE_APP_ROLE in str(settings.DATABASE_URL)
