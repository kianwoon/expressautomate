"""A job order can exist without an email, and knows whose it is."""

import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.models.opportunity import Opportunity
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user


async def test_manual_opportunity_needs_no_email() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(Opportunity).values(
                    id=opportunity_id,
                    tenant_id=tenant_id,
                    email_message_id=None,
                    source=Opportunity.MANUAL,
                    assigned_user_id=user_id,
                    job_title_raw="Warehouse Assistant",
                )
            )
        async with tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(Opportunity.source, Opportunity.assigned_user_id).where(
                        Opportunity.id == opportunity_id
                    )
                )
            ).one()
        assert row.source == "manual"
        assert row.assigned_user_id == user_id
    finally:
        await cleanup_tenant(tenant_id)


async def test_source_vocabulary_is_pinned() -> None:
    tenant_id, _user_id = await seed_tenant_with_user()
    try:
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(
                    insert(Opportunity).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        email_message_id=None,
                        source="shared",  # never a valid source: sharing creates no row
                    )
                )
    finally:
        await cleanup_tenant(tenant_id)


async def test_deleting_the_assignee_queues_the_job_order() -> None:
    """SET NULL, not CASCADE: a recruiter leaving must not delete the work."""
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(Opportunity).values(
                    id=opportunity_id,
                    tenant_id=tenant_id,
                    email_message_id=None,
                    source=Opportunity.MANUAL,
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
                    select(Opportunity.assigned_user_id).where(
                        Opportunity.id == opportunity_id
                    )
                )
            ).scalar_one()
        assert assigned is None
    finally:
        await cleanup_tenant(tenant_id)
