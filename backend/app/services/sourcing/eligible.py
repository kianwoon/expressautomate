"""Who may be ranked against a client's job order.

Read-only, like `candidate_matching`. It answers one question — is this person
even a candidate here — and leaves scoring, redaction and shortlisting to the
steps that follow.

Three rules, and the third is the interesting one:

- the record is `active`, because archived and merged rows are not people a
  recruiter can propose;
- the person is not `placed`, because they already have a job;
- the person has not already been put in front of *this client*, because
  proposing the same person to the same client twice is the mistake the
  `candidate_submissions` unique key exists to make impossible.

`rejected` candidates are deliberately kept. A rejection was recorded against
one role for one client; treating it as a permanent mark would quietly shrink
the database every time a client said no, which is the opposite of what an
agency's candidate pool is for.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate

# `tenant_id` is bound explicitly as well as being enforced by RLS. The policy
# is the boundary; this makes the query say out loud which agency it is for,
# and keeps it correct if it is ever run on an admin connection.
_ELIGIBLE = text(
    """
    SELECT c.id
    FROM candidates c
    WHERE c.tenant_id = :tenant_id
      AND c.record_status = :active
      AND c.pipeline_stage <> :placed
      AND NOT EXISTS (
          SELECT 1
          FROM candidate_submissions s
          WHERE s.tenant_id = c.tenant_id
            AND s.candidate_id = c.id
            AND s.client_id = :client_id
      )
    ORDER BY c.id
    """
)

# The same question with the third rule removed, for a job order whose client
# could not be resolved (`client_resolution.py`). A separate statement rather
# than binding NULL into the one above: `s.client_id = NULL` is never true, so
# the two do return the same rows, but it returns them by accident of
# three-valued logic — and a reader checking whether the exclusion applies
# would have to work that out. The caller says which question it is asking.
#
# allow-hardcode: a SQL statement, not a phrase list.
_ELIGIBLE_ANY_CLIENT = text(
    """
    SELECT c.id
    FROM candidates c
    WHERE c.tenant_id = :tenant_id
      AND c.record_status = :active
      AND c.pipeline_stage <> :placed
    ORDER BY c.id
    """
)


async def eligible_candidates(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """The candidate ids that may be ranked for this client, oldest key first.

    Ids rather than rows: the scorer needs roles and skills as well, and the
    step that loads those should decide what it fetches. A stable `ORDER BY`
    so a rerun of the same job considers the same people in the same order.

    `client_id=None` means the caller could not tell which client this job
    order is for, and the third rule is dropped for this run. That is the
    honest answer — the submissions rule can only exclude somebody already put
    in front of a *particular* client — but it is also the quiet one, which is
    why the run that asks for it records `client_unresolved_reason` alongside.
    """
    result = await session.execute(
        _ELIGIBLE if client_id is not None else _ELIGIBLE_ANY_CLIENT,
        {
            "tenant_id": tenant_id,
            "active": Candidate.ACTIVE,
            "placed": Candidate.PLACED,
            **({"client_id": client_id} if client_id is not None else {}),
        },
    )
    return [row[0] for row in result]
