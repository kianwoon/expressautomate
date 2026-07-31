"""Who, inside one agency, may see and edit a job order or a candidate.

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

from app.models.candidate import Candidate
from app.models.candidate_share import CandidateShare
from app.models.email_message import EmailMessage
from app.models.mailbox import Mailbox
from app.models.opportunity import Opportunity
from app.models.opportunity_share import OpportunityShare

OWNER_ROLE = "owner"


def shared_with_me_exists(user_id: uuid.UUID) -> ColumnElement[bool]:
    """A share that reaches `user_id` — a named share or a tenant broadcast.

    The single source of truth for "shared with me". `visible_opportunities`
    below, the list payload's row badge and the `scope=shared_with_me` filter
    in `app/api/opportunities.py` all call this, so none of them can drift
    from the others when sharing semantics change.
    """
    return (
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


def visible_opportunities(user_id: uuid.UUID, role: str) -> ColumnElement[bool]:
    """A WHERE clause, not a query — so it composes with existing sort,
    search and pagination without the caller changing shape.
    """
    if role == OWNER_ROLE:
        # A three-person agency needs the boss to see the pipeline.
        return true_()

    shared_with_me = shared_with_me_exists(user_id)

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


def candidate_shared_with_me_exists(user_id: uuid.UUID) -> ColumnElement[bool]:
    """A share that reaches `user_id` — a named share or a tenant broadcast.

    The single source of truth for "shared with me" on candidates, for the
    same reason the opportunity version is: the predicate, the list payload's
    row badge and the `scope=shared_with_me` filter all call this, so none of
    them can drift from the others.
    """
    return (
        select(CandidateShare.id)
        .where(CandidateShare.candidate_id == Candidate.id)
        .where(
            or_(
                CandidateShare.scope == CandidateShare.SCOPE_TENANT,
                and_(
                    CandidateShare.scope == CandidateShare.SCOPE_USER,
                    CandidateShare.shared_with_user_id == user_id,
                ),
            )
        )
        .exists()
    )


def visible_candidates(user_id: uuid.UUID, role: str) -> ColumnElement[bool]:
    """A WHERE clause, not a query.

    There is no mailbox term, unlike `visible_opportunities`. Candidates never
    arrive from the email pipeline, so no recipient has a prior claim on one.
    """
    if role == OWNER_ROLE:
        return true_()

    return or_(
        Candidate.owner_id.is_(None),  # the unclaimed queue
        Candidate.owner_id == user_id,
        candidate_shared_with_me_exists(user_id),
    )


def candidate_scope(scope: str, user_id: uuid.UUID) -> ColumnElement[bool]:
    """A filter WITHIN what `visible_candidates` already allows.

    It lives here, beside the predicate, rather than in `app/api/candidates.py`
    for two reasons. That module is at the repo's 1500-line ceiling; and
    `scope=shared_with_me` must be the *same* expression the predicate uses,
    so it calls `candidate_shared_with_me_exists` rather than restating the OR.

    Every caller ANDs this onto the predicate with a further `.where`, never
    substituting it — which is what makes "a scope cannot widen visibility"
    true by construction rather than by review. An unknown scope returns
    `true_()`, so the predicate alone still decides.
    """
    if scope == "mine":
        return Candidate.owner_id == user_id
    if scope == "queue":
        return Candidate.owner_id.is_(None)
    if scope == "shared_with_me":
        return candidate_shared_with_me_exists(user_id)
    return true_()


def can_edit_candidate(candidate: Candidate, user_id: uuid.UUID, role: str) -> bool:
    """An unowned candidate is visible and claimable but NOT editable.

    Claiming it is the act that makes it editable. A row nobody has taken
    responsibility for is where a wrong edit is least likely to be noticed.
    """
    if role == OWNER_ROLE:
        return True
    return candidate.owner_id == user_id


async def load_visible_candidate(
    session: AsyncSession, candidate_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> Candidate:
    """404, never 403 — a 403 would confirm the row exists."""
    row = (
        await session.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .where(visible_candidates(user_id, role))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such candidate.")
    return row


async def load_editable_candidate(
    session: AsyncSession, candidate_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> Candidate:
    """403 when visible but not editable.

    The caller can already see this candidate, so concealing its existence
    would be theatre, and a 404 would tell a recruiter that a colleague's
    shared candidate had vanished.
    """
    row = await load_visible_candidate(session, candidate_id, user_id, role)
    if not can_edit_candidate(row, user_id, role):
        raise HTTPException(
            status_code=403,
            detail="This candidate is shared with you, not assigned to you.",
        )
    return row
