"""A grant of sight on someone else's job order."""

import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.models.opportunity import Opportunity
from app.models.opportunity_share import OpportunityShare
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user


async def _an_opportunity(tenant_id: uuid.UUID) -> uuid.UUID:
    opportunity_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        await session.execute(
            insert(Opportunity).values(
                id=opportunity_id,
                tenant_id=tenant_id,
                email_message_id=None,
                source=Opportunity.MANUAL,
            )
        )
    return opportunity_id


async def test_tenant_scope_forbids_a_target_user() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = await _an_opportunity(tenant_id)
    try:
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(
                    insert(OpportunityShare).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        opportunity_id=opportunity_id,
                        scope=OpportunityShare.SCOPE_TENANT,
                        shared_with_user_id=user_id,  # must be NULL for tenant scope
                    )
                )
    finally:
        await cleanup_tenant(tenant_id)


async def test_user_scope_requires_a_target_user() -> None:
    tenant_id, _user_id = await seed_tenant_with_user()
    opportunity_id = await _an_opportunity(tenant_id)
    try:
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(
                    insert(OpportunityShare).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        opportunity_id=opportunity_id,
                        scope=OpportunityShare.SCOPE_USER,
                        shared_with_user_id=None,
                    )
                )
    finally:
        await cleanup_tenant(tenant_id)


async def test_deleting_the_recipient_deletes_the_share() -> None:
    """CASCADE, not SET NULL.

    SET NULL would turn a user share into a tenant broadcast, and would
    violate ck_opportunity_shares_scope_target — making the user DELETE fail
    outright rather than merely doing the wrong thing.
    """
    tenant_id, owner_id = await seed_tenant_with_user()
    # `seed_tenant_with_user` always makes a tenant of its own, so the second
    # call leaves one behind once the recipient is moved across. Kept and
    # dropped in the `finally` below — an orphan tenant per run is the kind of
    # residue that eventually makes a "why is this row here" afternoon.
    spare_tenant_id, recipient_id = await seed_tenant_with_user()
    # Put the recipient in the same tenant as the owner.
    async with AdminSessionLocal() as session:
        await session.execute(
            text("UPDATE users SET tenant_id = :t WHERE id = :u"),
            {"t": tenant_id, "u": recipient_id},
        )
        await session.commit()
    opportunity_id = await _an_opportunity(tenant_id)
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(OpportunityShare).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_USER,
                    shared_with_user_id=recipient_id,
                    shared_by_user_id=owner_id,
                )
            )
        async with AdminSessionLocal() as session:
            await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": recipient_id})
            await session.commit()
        async with tenant_session(tenant_id) as session:
            remaining = (
                await session.execute(
                    select(OpportunityShare.id).where(
                        OpportunityShare.opportunity_id == opportunity_id
                    )
                )
            ).scalars().all()
        assert remaining == []
    finally:
        await cleanup_tenant(tenant_id, spare_tenant_id)


async def test_resharing_to_the_same_user_is_refused_by_the_index() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = await _an_opportunity(tenant_id)
    values = dict(
        tenant_id=tenant_id,
        opportunity_id=opportunity_id,
        scope=OpportunityShare.SCOPE_USER,
        shared_with_user_id=user_id,
    )
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(insert(OpportunityShare).values(id=uuid.uuid4(), **values))
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(insert(OpportunityShare).values(id=uuid.uuid4(), **values))
    finally:
        await cleanup_tenant(tenant_id)
