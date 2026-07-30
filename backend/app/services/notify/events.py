"""What happened, in a form that has not yet chosen a channel.

Constants rather than free strings at the call sites, for the same reason
`sync_event.py` gives: subscriptions are stored on this value, so a typo in one
producer becomes a category nobody is subscribed to rather than an error.
"""

import uuid
from dataclasses import dataclass

EVENT_OPPORTUNITY_NEW = "opportunity.new"
EVENT_OPPORTUNITY_NEEDS_REVIEW = "opportunity.needs_review"
EVENT_OPPORTUNITY_SHARED = "opportunity.shared"
EVENT_OPPORTUNITY_ASSIGNED = "opportunity.assigned"

ALL_EVENT_KINDS: tuple[str, ...] = (
    EVENT_OPPORTUNITY_NEW,
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_SHARED,
    EVENT_OPPORTUNITY_ASSIGNED,
)

# What an absent value reads as. The AI must not fabricate one (plan §15), and
# a blank in a WhatsApp template parameter is rejected by Meta outright.
MISSING = "Not mentioned"


@dataclass(frozen=True)
class OpportunityEvent:
    """One vacancy, denormalised at emit time.

    The fields are copied rather than looked up later on purpose: by the time
    the delivery job runs, the opportunity may have been edited or deleted, and
    a notification should describe what happened when it happened.
    """

    kind: str
    tenant_id: uuid.UUID
    opportunity_id: uuid.UUID
    job_title: str | None
    company_name: str | None
    location: str | None
    salary: str | None
    # Who should hear about this. `None` keeps the original tenant-wide
    # meaning, so nothing already in the catalogue changes: a broadcast share
    # and an unassigned job order both legitimately concern everybody.
    recipient_user_ids: tuple[uuid.UUID, ...] | None = None
    # Set on opportunity.shared and opportunity.assigned.
    actor_name: str | None = None
    note: str | None = None
