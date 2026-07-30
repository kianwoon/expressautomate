"""Who, inside one agency, may see and edit a job order.

RLS enforces the boundary between agencies and always will: that boundary is
hard, permanent, and belongs in the database. The boundary between two
recruiters at the same agency is a product rule that will move as the product
moves, and encoding it as an RLS policy would mean a migration every time
sharing semantics change.

The cost of that choice is that this predicate lives in application code and
can be forgotten. Two things contain it: every by-id read goes through
`load_visible_opportunity`, and `tests/test_opportunity_routes_guarded.py`
asserts structurally that no route escapes it.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy import true as true_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_message import EmailMessage
from app.models.mailbox import Mailbox
from app.models.opportunity import Opportunity
from app.models.opportunity_share import OpportunityShare

OWNER_ROLE = "owner"


def visible_opportunities(user_id: uuid.UUID, role: str) -> ColumnElement[bool]:
    """A WHERE clause, not a query — so it composes with existing sort,
    search and pagination without the caller changing shape.
    """
    if role == OWNER_ROLE:
        # A three-person agency needs the boss to see the pipeline.
        return true_()

    shared_with_me = (
        select(OpportunityShare.id)
        .where(OpportunityShare.opportunity_id == Opportunity.id)
        .where(
            or_(
                OpportunityShare.scope == OpportunityShare.SCOPE_TENANT,
                and_(
                    OpportunityShare.scope == OpportunityShare.SCOPE_USER,
                    OpportunityShare.shared_with_user_id == user_id,
                ),
            )
        )
        .exists()
    )

    # The recipient of the original mail keeps sight of what was extracted
    # from it. They have the email in Outlook; hiding the extracted version
    # of a message they can already read reads as a bug.
    mine_by_mailbox = (
        select(EmailMessage.id)
        .join(Mailbox, Mailbox.id == EmailMessage.mailbox_id)
        .where(EmailMessage.id == Opportunity.email_message_id)
        .where(Mailbox.user_id == user_id)
        .exists()
    )

    return or_(
        Opportunity.assigned_user_id.is_(None),  # the unassigned queue
        Opportunity.assigned_user_id == user_id,
        shared_with_me,
        mine_by_mailbox,
    )


def can_edit(opportunity: Opportunity, user_id: uuid.UUID, role: str) -> bool:
    """Narrower than visibility, and deliberately so.

    An unassigned job order is visible and claimable but NOT editable:
    claiming it is the act that makes it editable. Letting anyone edit a row
    nobody has taken responsibility for is the state where a wrong edit is
    least likely to be noticed.
    """
    if role == OWNER_ROLE:
        return True
    return opportunity.assigned_user_id == user_id


async def load_visible_opportunity(
    session: AsyncSession, opportunity_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> Opportunity:
    """404, never 403 — a 403 would confirm the row exists."""
    row = (
        await session.execute(
            select(Opportunity)
            .where(Opportunity.id == opportunity_id)
            .where(visible_opportunities(user_id, role))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such job order.")
    return row


async def load_editable_opportunity(
    session: AsyncSession, opportunity_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> Opportunity:
    """403 when visible but not editable.

    The opposite of the rule above, for the opposite reason: the caller can
    already see this job order, so concealing its existence would be theatre,
    and a 404 would tell a recruiter their colleague's shared job order had
    vanished.
    """
    row = await load_visible_opportunity(session, opportunity_id, user_id, role)
    if not can_edit(row, user_id, role):
        raise HTTPException(
            status_code=403, detail="This job order is shared with you, not assigned to you."
        )
    return row
