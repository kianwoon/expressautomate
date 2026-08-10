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

**Revisions are not duplicates.** A later email that *changes* an open job
order's requirements — female/Chinese-only becomes open to male and all races,
say — is a revision, not a re-forward. The write path (`ingest/persist.py`)
detects the change and points the old row's
`superseded_by_opportunity_id` at the new one. This module then hides the
superseded row in favour of its successor, and a row that is the *successor*
of a supersede link is never hidden as a duplicate — it is the current
revision, and hiding it would put the stale requirements back on the list.
"""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select, Subquery

from app.models import EmailMessage, Opportunity


def duplicate_opportunity_ids(visible) -> Subquery:
    """A subquery selecting the ids of job orders to hide.

    Composable: a caller writes `.where(Opportunity.id.not_in(dupes))` to drop
    them from a count or a list, both built differently in different modules.

    `visible` is the per-recruiter visibility clause (`visible_opportunities`),
    applied to the earliest-instance computation so a job order the reader
    cannot see cannot determine what they are shown — the same scoping the job
    orders list itself applies.

    Two kinds of row are hidden:

    - **Superseded rows** (`superseded_by_opportunity_id IS NOT NULL`): a later
      email revised this job order, and its successor is the current one.
    - **Later re-forwards** of an open job order, whose conversation has an
      earlier current instance — but **not** rows that are themselves the
      successor of a supersede link. A successor is the current revision; it is
      a later email in the conversation, so without this exclusion the plain
      re-forward rule would hide exactly the row the client just updated.
    """
    email = aliased(EmailMessage)
    # Any row whose `superseded_by_opportunity_id` points at a given row makes
    # that row a successor — the current revision — and therefore not a
    # duplicate. Correlated per candidate row below.
    predecessor = aliased(Opportunity)
    is_successor = (
        select(1)
        .select_from(predecessor)
        .where(predecessor.superseded_by_opportunity_id == Opportunity.id)
        .exists()
    )

    # The earliest received_datetime per conversation — but only for
    # conversations whose earliest *current* row is still open. A conversation
    # whose earliest instance has been placed is done, and later rows in it are
    # new work, not duplicates. A superseded row is not an anchor: it has been
    # replaced, so the revision that replaced it is the earliest current
    # instance, and using the superseded row's date would hide the successor.
    open_earliest = (
        select(
            email.conversation_id.label("conversation_id"),
            func.min(email.received_datetime).label("earliest_at"),
        )
        .select_from(Opportunity)
        .join(email, email.id == Opportunity.email_message_id)
        .where(visible)
        .where(Opportunity.placement_type.is_(None))
        .where(Opportunity.superseded_by_opportunity_id.is_(None))
        .where(email.conversation_id.is_not(None))
        .group_by(email.conversation_id)
    ).subquery()

    # Rows to hide: superseded rows, and rows whose conversation has an earlier
    # email than their own — the later re-forwards — unless they are the
    # successor of a supersede link (the current revision).
    dupes: Select = (
        select(Opportunity.id)
        .select_from(Opportunity)
        .join(email, email.id == Opportunity.email_message_id)
        .join(
            open_earliest,
            open_earliest.c.conversation_id == email.conversation_id,
        )
        .where(
            or_(
                Opportunity.superseded_by_opportunity_id.is_not(None),
                and_(
                    email.received_datetime > open_earliest.c.earliest_at,
                    Opportunity.placement_type.is_(None),
                    ~is_successor,
                ),
            )
        )
    )
    return dupes.subquery()
