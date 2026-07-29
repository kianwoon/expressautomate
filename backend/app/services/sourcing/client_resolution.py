"""Which client a job order is for — or an honest admission that we cannot tell.

Eligibility's third rule is "not already submitted to *this client*", and the
spec that wrote it assumed a job order knows its client. It does not: there is
no `opportunities.client_id`. The only link is the email the job order arrived
on and the `client_mentions` the matcher wrote against that email.

That link is weaker than a column, in two specific ways this module is built
around:

1. **A mention is not a certainty.** `client_mentions.matched_by` is either
   `email_domain` — a fact about where the mail came from — or `name`, which
   `ClientMention`'s own docstring calls a resemblance. A domain match is
   therefore always preferred, and a name match is only trusted when it is the
   only thing on offer.
2. **One email can mention two clients.** Picking the oldest of them, which is
   what the first version did, is picking at random with a confident face. The
   cost of picking wrong is not a missing exclusion but a *wrong* one: a
   candidate never submitted anywhere near this client is dropped from the
   shortlist and nobody sees them.

So genuine ambiguity is treated as no answer at all. The caller runs the
shortlist anyway — refusing would kill the feature for every job order whose
client was never matched — and records `reason` on the run so the recruiter is
told the already-submitted check did not run, rather than being left to
discover it by re-pitching somebody.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The two values `client_matching._resolve` writes. Named here rather than
# spelled inline so the ranking below is legible as a ranking.
DOMAIN_MATCH = "email_domain"
NAME_MATCH = "name"

# allow-hardcode: a SQL statement, not a phrase list.
#
# DISTINCT because two mentions of the same client — the matcher runs again
# after a crash, and a merge moves mentions onto the surviving row — are one
# client, not an ambiguity. `tenant_id` is bound as well as enforced by RLS,
# for the same reason `eligible.py` binds it: the query should say out loud
# which agency it is for.
_MENTIONS_FOR_OPPORTUNITY = text(
    """
    SELECT DISTINCT m.client_id, m.matched_by
    FROM client_mentions m
    JOIN opportunities o
      ON o.email_message_id = m.email_message_id
     AND o.tenant_id = m.tenant_id
    WHERE o.id = :opportunity_id
      AND o.tenant_id = :tenant_id
    """
)

# allow-hardcode: sentences shown to a recruiter, not configuration.
_NO_MENTION = (
    "The already-submitted check did not run: this job order's email is not "
    "linked to any client, so there is nobody to have submitted to. Link a "
    "client to see candidates you have already put in front of them excluded."
)
_AMBIGUOUS = (
    "The already-submitted check did not run: this job order's email mentions "
    "more than one client and none of them is a clear match, so excluding "
    "against one of them could have hidden a candidate wrongly."
)


@dataclass(frozen=True)
class ClientResolution:
    """The client to exclude against, or the sentence explaining why not.

    Exactly one of the two is set. A resolution carrying neither would be the
    silent-skip this module exists to remove.
    """

    client_id: uuid.UUID | None
    reason: str | None


async def resolve_client(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    opportunity_id: uuid.UUID,
) -> ClientResolution:
    """The one client this job order is for, if there is exactly one.

    A domain match wins outright: if a single client was matched by domain it
    is the answer even when other clients were matched by name off the same
    email, because the sender's domain is evidence of a different kind from a
    name appearing in a body. Two domain matches on one email is a real
    ambiguity and is refused rather than broken by a tiebreak — there is no
    column that would make one of them more true than the other.
    """
    rows = (
        await session.execute(
            _MENTIONS_FOR_OPPORTUNITY,
            {"opportunity_id": opportunity_id, "tenant_id": tenant_id},
        )
    ).all()
    if not rows:
        return ClientResolution(client_id=None, reason=_NO_MENTION)

    by_domain = {row.client_id for row in rows if row.matched_by == DOMAIN_MATCH}
    if len(by_domain) == 1:
        return ClientResolution(client_id=by_domain.pop(), reason=None)
    if by_domain:
        return ClientResolution(client_id=None, reason=_AMBIGUOUS)

    by_name = {row.client_id for row in rows if row.matched_by == NAME_MATCH}
    if len(by_name) == 1:
        return ClientResolution(client_id=by_name.pop(), reason=None)
    return ClientResolution(client_id=None, reason=_AMBIGUOUS)
