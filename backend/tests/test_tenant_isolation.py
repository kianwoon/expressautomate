"""Tenant/User schema integrity — the constraints multi-tenancy rests on (§18).

These caught a real defect: `User.tenant_id` was originally declared without a
ForeignKey, which left `Tenant.users` with no join condition
(NoForeignKeysError on first ORM use) and let the database accept orphan
tenant_ids. Core-only health checks did not exercise the mapper, so nothing
surfaced it.

They run through `admin_session` (the schema owner) on purpose: these assert
*schema* guarantees, and RLS would otherwise mask a missing constraint behind
an empty result set. Isolation itself is proven in test_rls.py against the
restricted role.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import configure_mappers, selectinload

from app.models import Tenant, User
from tests.conftest import AdminSessionLocal


def _tenant() -> Tenant:
    return Tenant(name="Acme Recruitment", slug=f"acme-{uuid.uuid4().hex[:8]}")


def test_mappers_configure() -> None:
    """Fails loudly if a relationship loses its join condition."""
    configure_mappers()


async def test_tenant_user_relationship_round_trips(admin_session: AsyncSession) -> None:
    t = _tenant()
    admin_session.add(t)
    await admin_session.flush()
    admin_session.add(User(tenant_id=t.id, email="recruiter@acme.sg", display_name="R"))
    await admin_session.commit()
    tenant_id = t.id

    try:
        loaded = (
            await admin_session.execute(
                select(Tenant).options(selectinload(Tenant.users)).where(Tenant.id == tenant_id)
            )
        ).scalar_one()
        assert [u.email for u in loaded.users] == ["recruiter@acme.sg"]
    finally:
        await _drop_tenant(tenant_id)


async def test_user_cannot_reference_a_nonexistent_tenant(admin_session: AsyncSession) -> None:
    """The FK is what stops rows being written under a tenant that never existed."""
    admin_session.add(User(tenant_id=uuid.uuid4(), email="ghost@nowhere.sg"))
    with pytest.raises(IntegrityError):
        await admin_session.commit()


async def test_deleting_a_tenant_removes_its_users(admin_session: AsyncSession) -> None:
    """Supports the §30 tenant deletion workflow — no rows left behind."""
    t = _tenant()
    admin_session.add(t)
    await admin_session.flush()
    admin_session.add(User(tenant_id=t.id, email="gone@acme.sg"))
    await admin_session.commit()
    tenant_id = t.id

    await _drop_tenant(tenant_id)

    async with AdminSessionLocal() as s:
        remaining = (
            await s.execute(select(User).where(User.tenant_id == tenant_id))
        ).scalars().all()
    assert remaining == []


async def test_email_is_unique_per_tenant_but_not_globally() -> None:
    """The same person may appear in two agencies; twice in one is a mistake."""
    async with AdminSessionLocal() as s:
        a, b = _tenant(), _tenant()
        s.add_all([a, b])
        await s.flush()
        s.add_all(
            [User(tenant_id=a.id, email="shared@x.sg"), User(tenant_id=b.id, email="shared@x.sg")]
        )
        await s.commit()
        ids = (a.id, b.id)

    try:
        async with AdminSessionLocal() as s:
            s.add(User(tenant_id=ids[0], email="shared@x.sg"))
            with pytest.raises(IntegrityError):
                await s.commit()
    finally:
        for tid in ids:
            await _drop_tenant(tid)


async def _drop_tenant(tenant_id: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        found = (
            await s.execute(
                select(Tenant).options(selectinload(Tenant.users)).where(Tenant.id == tenant_id)
            )
        ).scalar_one_or_none()
        if found is not None:
            await s.delete(found)
            await s.commit()
