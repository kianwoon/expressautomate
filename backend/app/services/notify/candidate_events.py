"""What the agency is told about a candidate.

A separate dataclass rather than a widened `OpportunityEvent`: that type's
`job_title`, `company_name`, `location` and `salary` feed the WhatsApp
templates, and a candidate has none of them. Widening it would put four
permanently-None fields in front of every template author.

What the dispatch machinery actually keys on is narrow — `kind`, `tenant_id`,
`recipient_user_ids`, and one subject id — so the two types meet at that
protocol and nowhere else.
"""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateEvent:
    kind: str
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None
    # Who should hear about this. `None` keeps the tenant-wide meaning, so a
    # broadcast is one event with N recipients rather than N events — which is
    # what keeps the per-subscriber hourly cap behaving.
    recipient_user_ids: tuple[uuid.UUID, ...] | None = None
    # Who did it, and why. Both are denormalised at emit time and are NOT
    # recoverable by the delivery worker, which rebuilds the event from the
    # outbox row and the candidate row alone — see `deliver_notification`.
    actor_name: str | None = None
    note: str | None = None

    @property
    def subject_id(self) -> uuid.UUID:
        return self.candidate_id
