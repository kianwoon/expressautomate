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

# Candidates. Absorbed as constants with no migration — `event_kind` is a
# String(48) rather than an enum precisely so a new kind costs nothing.
CANDIDATE_SHARED = "candidate.shared"
CANDIDATE_ASSIGNED = "candidate.assigned"
# Releasing to the queue tells the agency: a released candidate is queue work
# again, and nobody would otherwise learn it is available.
CANDIDATE_UNCLAIMED = "candidate.unclaimed"
CANDIDATE_ACCESS_REQUESTED = "candidate.access_requested"
CANDIDATE_ACCESS_GRANTED = "candidate.access_granted"
# Not optional politeness. A request that silently never resolves leaves the
# requester believing it is pending, and they ask again.
CANDIDATE_ACCESS_DECLINED = "candidate.access_declined"

# What the delivery worker keys on to decide which subject table to read and
# which event type to rebuild. A prefix rather than a membership test so a
# kind added later is routed correctly before anyone remembers this list.
CANDIDATE_KIND_PREFIX = "candidate."

CANDIDATE_EVENT_KINDS: tuple[str, ...] = (
    CANDIDATE_SHARED,
    CANDIDATE_ASSIGNED,
    CANDIDATE_UNCLAIMED,
    CANDIDATE_ACCESS_REQUESTED,
    CANDIDATE_ACCESS_GRANTED,
    CANDIDATE_ACCESS_DECLINED,
)

# Every kind anyone may subscribe to. A kind missing here has no subscribers,
# so `_write_rows` finds nothing and the event delivers to nobody — which
# looks exactly like the feature working quietly.
ALL_EVENT_KINDS: tuple[str, ...] = (
    EVENT_OPPORTUNITY_NEW,
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_SHARED,
    EVENT_OPPORTUNITY_ASSIGNED,
    *CANDIDATE_EVENT_KINDS,
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

    @property
    def subject_id(self) -> uuid.UUID:
        """The one id the dispatcher writes to `notification_deliveries`.

        Named the same on `CandidateEvent`: that narrow protocol — kind,
        tenant_id, recipient_user_ids, subject_id — is the whole surface the
        two event types share.
        """
        return self.opportunity_id
