"""Who may see a job order, term by term.

RLS covers the tenant boundary; this predicate covers the boundary between
recruiters inside one agency, and it lives in application code — so it is
tested exhaustively rather than trusted.
"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import insert, select, text

from app.db.rls import tenant_session
from app.models.email_message import EmailMessage
from app.models.mailbox import Mailbox
from app.models.opportunity import Opportunity
from app.models.opportunity_share import OpportunityShare
from app.services.visibility import (
    OWNER_ROLE,
    can_edit,
    load_editable_opportunity,
    load_visible_opportunity,
    visible_opportunities,
)
from tests.conftest import AdminSessionLocal, cleanup_tenant, seed_tenant_with_user


async def _second_user(tenant_id: uuid.UUID, role: str = "recruiter") -> uuid.UUID:
    user_id = uuid.uuid4()
    async with AdminSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:id, :t, :e, :r)"
            ),
            {"id": user_id, "t": tenant_id, "e": f"{user_id.hex[:8]}@example.test", "r": role},
        )
        await session.commit()
    return user_id


async def _opportunity(tenant_id: uuid.UUID, **values) -> uuid.UUID:
    opportunity_id = uuid.uuid4()
    values.setdefault("email_message_id", None)
    values.setdefault("source", Opportunity.MANUAL)
    async with tenant_session(tenant_id) as session:
        await session.execute(
            insert(Opportunity).values(
                id=opportunity_id,
                tenant_id=tenant_id,
                **values,
            )
        )
    return opportunity_id


async def _mailbox(tenant_id: uuid.UUID, user_id: uuid.UUID | None) -> uuid.UUID:
    mailbox_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        await session.execute(
            insert(Mailbox).values(
                id=mailbox_id,
                tenant_id=tenant_id,
                user_id=user_id,
                ms_user_id=mailbox_id.hex[:8],
                scope="whole_inbox",
                folder_id=mailbox_id.hex[:8],
                retention_months=12,
            )
        )
    return mailbox_id


async def _email_message(tenant_id: uuid.UUID, mailbox_id: uuid.UUID) -> uuid.UUID:
    email_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        await session.execute(
            insert(EmailMessage).values(
                id=email_id,
                tenant_id=tenant_id,
                mailbox_id=mailbox_id,
                graph_message_id=email_id.hex[:8],
            )
        )
    return email_id


async def _visible_ids(tenant_id, user_id, role) -> set[uuid.UUID]:
    async with tenant_session(tenant_id) as session:
        rows = await session.execute(
            select(Opportunity.id).where(visible_opportunities(user_id, role))
        )
    return set(rows.scalars().all())


async def test_unassigned_is_visible_to_everyone() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        assert opportunity_id in await _visible_ids(tenant_id, other, "recruiter")
        assert opportunity_id in await _visible_ids(tenant_id, user_id, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_assigned_is_hidden_from_everyone_else() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        assert opportunity_id in await _visible_ids(tenant_id, mine, "recruiter")
        assert opportunity_id not in await _visible_ids(tenant_id, other, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_user_share_reveals_it() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(OpportunityShare).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_USER,
                    shared_with_user_id=other,
                    shared_by_user_id=mine,
                )
            )
        assert opportunity_id in await _visible_ids(tenant_id, other, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_tenant_share_reveals_it_to_a_user_who_did_not_exist_yet() -> None:
    """The reason a broadcast is one row and not N."""
    tenant_id, mine = await seed_tenant_with_user()
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(OpportunityShare).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_TENANT,
                    shared_with_user_id=None,
                    shared_by_user_id=mine,
                )
            )
        newcomer = await _second_user(tenant_id)  # hired after the broadcast
        assert opportunity_id in await _visible_ids(tenant_id, newcomer, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_the_owner_sees_everything() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    boss = await _second_user(tenant_id, role=OWNER_ROLE)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        assert opportunity_id in await _visible_ids(tenant_id, boss, OWNER_ROLE)
    finally:
        await cleanup_tenant(tenant_id)


async def test_an_invisible_job_order_is_404_not_403() -> None:
    """A 403 would confirm the row exists."""
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        async with tenant_session(tenant_id) as session:
            with pytest.raises(HTTPException) as exc:
                await load_visible_opportunity(session, opportunity_id, other, "recruiter")
        assert exc.value.status_code == 404
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_share_recipient_may_read_but_not_edit() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(OpportunityShare).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_USER,
                    shared_with_user_id=other,
                    shared_by_user_id=mine,
                )
            )
        async with tenant_session(tenant_id) as session:
            await load_visible_opportunity(session, opportunity_id, other, "recruiter")
            with pytest.raises(HTTPException) as exc:
                await load_editable_opportunity(session, opportunity_id, other, "recruiter")
        # Visible, so hiding its existence would be theatre.
        assert exc.value.status_code == 403
    finally:
        await cleanup_tenant(tenant_id)


async def test_unassigned_is_readable_by_all_and_editable_by_none() -> None:
    tenant_id, _mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        async with tenant_session(tenant_id) as session:
            row = await load_visible_opportunity(session, opportunity_id, other, "recruiter")
            assert can_edit(row, other, "recruiter") is False
    finally:
        await cleanup_tenant(tenant_id)


async def test_owning_the_mailbox_reveals_a_job_order_assigned_to_someone_else() -> None:
    """The recipient of the original mail keeps sight of what was extracted
    from it, even when it was assigned away — they already have the email.
    """
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    mailbox_id = await _mailbox(tenant_id, user_id=mine)
    email_id = await _email_message(tenant_id, mailbox_id)
    opportunity_id = await _opportunity(
        tenant_id, assigned_user_id=other, email_message_id=email_id
    )
    try:
        assert opportunity_id in await _visible_ids(tenant_id, mine, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_owning_no_relevant_mailbox_does_not_reveal_it() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    mailbox_id = await _mailbox(tenant_id, user_id=other)
    email_id = await _email_message(tenant_id, mailbox_id)
    opportunity_id = await _opportunity(
        tenant_id, assigned_user_id=other, email_message_id=email_id
    )
    try:
        assert opportunity_id not in await _visible_ids(tenant_id, mine, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_mailbox_with_no_owner_is_not_visible_to_everyone() -> None:
    """`mailboxes.user_id` is nullable; a naive `== user_id` comparison
    against NULL must not make the row public.
    """
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    third = await _second_user(tenant_id)
    mailbox_id = await _mailbox(tenant_id, user_id=None)
    email_id = await _email_message(tenant_id, mailbox_id)
    opportunity_id = await _opportunity(
        tenant_id, assigned_user_id=third, email_message_id=email_id
    )
    try:
        assert opportunity_id not in await _visible_ids(tenant_id, mine, "recruiter")
        assert opportunity_id not in await _visible_ids(tenant_id, other, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_manual_job_order_is_not_reachable_via_mailbox_ownership() -> None:
    """`email_message_id IS NULL` on a manual job order must not accidentally
    join to some unrelated email via the mailbox term.
    """
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=other)
    try:
        assert opportunity_id not in await _visible_ids(tenant_id, mine, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)
