"""Collapse job orders that are a later re-forward of an open one already held.

A buddy or owner who forwards the same job order again days later creates a
second `Opportunity` row: a different email, a different `received_datetime`,
but the same Graph `conversation_id` (a forward keeps the conversation it came
from). Both the job orders list and the buddies referral count/list need to
collapse those later re-forwards, so the rule lives here once rather than in
two modules that drift.

The signal is `conversation_id`, not `internet_message_id` — verified against
production data: `internet_message_id` never survives a forward (0 repeats
across 80 job orders), but `conversation_id` does (7 duplicate groups, all
unfulfilled re-forwards).

Partitioned by `(conversation_id, received_datetime)`, not by title:
`job_title_normalized` is NULL for every row in production, and one forwarded
email can list several distinct roles (all sharing one arrival moment). The
dedupe keeps every row from the *earliest* email in a conversation and hides
only rows from a *later* email — so a multi-role forward is not collapsed to
one row, but the same single role sent again next week is.

Only hides when the earliest instance is still open (`placement_type IS NULL`):
a role that has since been placed is finished, and a re-forward of it is a new
hire, not a duplicate.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select, Subquery

from app.models import EmailMessage, Opportunity


def duplicate_opportunity_ids(visible) -> Subquery:
    """A subquery selecting the ids of job orders that are later re-forwards.

    Composable: a caller writes `.where(Opportunity.id.not_in(dupes))` to drop
    them from a count or a list, both built differently in different modules.

    `visible` is the per-recruiter visibility clause (`visible_opportunities`),
    applied to the earliest-instance computation so a job order the reader
    cannot see cannot determine what they are shown — the same scoping the job
    orders list itself applies.
    """
    email = aliased(EmailMessage)
    # The earliest received_datetime per conversation — but only for
    # conversations whose earliest row is still open. A conversation whose
    # earliest instance has been placed is done, and later rows in it are new
    # work, not duplicates.
    open_earliest = (
        select(
            email.conversation_id.label("conversation_id"),
            func.min(email.received_datetime).label("earliest_at"),
        )
        .select_from(Opportunity)
        .join(email, email.id == Opportunity.email_message_id)
        .where(visible)
        .where(Opportunity.placement_type.is_(None))
        .where(email.conversation_id.is_not(None))
        .group_by(email.conversation_id)
    ).subquery()

    # Rows whose conversation has an earlier email than their own: these are the
    # later re-forwards.
    dupes: Select = (
        select(Opportunity.id)
        .select_from(Opportunity)
        .join(email, email.id == Opportunity.email_message_id)
        .join(
            open_earliest,
            open_earliest.c.conversation_id == email.conversation_id,
        )
        .where(email.received_datetime > open_earliest.c.earliest_at)
        .where(Opportunity.placement_type.is_(None))
    )
    return dupes.subquery()
