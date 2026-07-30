"""A client belongs to a recruiter, and that reference cannot leave the agency."""

import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.models.client import Client, ClientCollaborator
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user


async def test_client_can_be_assigned_to_a_user() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    client_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(Client).values(
                    id=client_id,
                    tenant_id=tenant_id,
                    name="Acme Pte Ltd",
                    name_normalized="acme",
                    assigned_user_id=user_id,
                )
            )
        async with tenant_session(tenant_id) as session:
            assigned = (
                await session.execute(
                    select(Client.assigned_user_id).where(Client.id == client_id)
                )
            ).scalar_one()
        assert assigned == user_id
    finally:
        await cleanup_tenant(tenant_id)


async def test_assignee_from_another_tenant_is_refused() -> None:
    tenant_a, _user_a = await seed_tenant_with_user()
    tenant_b, user_b = await seed_tenant_with_user()
    try:
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_a) as session:
                await session.execute(
                    insert(Client).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_a,
                        name="Acme Pte Ltd",
                        name_normalized="acme",
                        assigned_user_id=user_b,  # belongs to tenant B
                    )
                )
    finally:
        await cleanup_tenant(tenant_a, tenant_b)


async def test_deleting_the_assignee_leaves_the_client_unassigned() -> None:
    """SET NULL, not corruption: a recruiter leaving must not break their clients."""
    tenant_id, user_id = await seed_tenant_with_user()
    client_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(Client).values(
                    id=client_id,
                    tenant_id=tenant_id,
                    name="Acme Pte Ltd",
                    name_normalized="acme",
                    assigned_user_id=user_id,
                )
            )
        async with AdminSessionLocal() as session:
            await session.execute(
                text("DELETE FROM users WHERE id = :u"), {"u": user_id}
            )
            await session.commit()
        async with tenant_session(tenant_id) as session:
            assigned = (
                await session.execute(
                    select(Client.assigned_user_id).where(Client.id == client_id)
                )
            ).scalar_one()
        assert assigned is None
    finally:
        await cleanup_tenant(tenant_id)


async def test_collaborator_is_unique_per_client_and_user() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    client_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(Client).values(
                    id=client_id,
                    tenant_id=tenant_id,
                    name="Acme Pte Ltd",
                    name_normalized="acme",
                )
            )
            await session.execute(
                insert(ClientCollaborator).values(
                    id=uuid.uuid4(), tenant_id=tenant_id, client_id=client_id, user_id=user_id
                )
            )
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(
                    insert(ClientCollaborator).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        client_id=client_id,
                        user_id=user_id,
                    )
                )
    finally:
        await cleanup_tenant(tenant_id)
