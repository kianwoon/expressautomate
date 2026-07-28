"""Deciding which existing person a set of details refers to.

Read-only, and deliberately so. It reports what it found and never writes, so
the manual create path and the later bulk import can share it while recording
completely different things about the outcome.

**This is not the client matcher's shape, and copying that would be a bug.**
`client_matching.py` resolves its race with `INSERT ... ON CONFLICT`, which
works because a client has exactly one identity key, so the statement can name
one arbiter index. Postgres allows only one arbiter. A candidate has two keys,
and a row that collides on the key the clause did not name raises a unique
violation the statement cannot absorb. So this is select-then-write, with the
unique indexes as the backstop against a race rather than the mechanism.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.candidate_naming import is_matchable_phone, normalize_email

# `merged` rows are excluded: their identity now belongs to the target. Archived
# rows are NOT excluded — they still hold the unique key, so skipping one would
# send the caller to an insert that collides with it.
_BY_EMAIL = text(
    """
    SELECT id FROM candidates
    WHERE tenant_id = :tenant_id AND lower(email) = :email AND record_status <> 'merged'
    LIMIT 1
    """
)

_BY_PHONE = text(
    """
    SELECT id FROM candidates
    WHERE tenant_id = :tenant_id AND phone_e164 = :phone AND record_status <> 'merged'
    LIMIT 1
    """
)


@dataclass(frozen=True)
class MatchResult:
    """What the matcher found. Exactly one of these situations holds.

    - `candidate_id` set: one person. `matched_on` says which key decided.
    - `conflict` set: two different people. The caller must not pick one.
    - both None: nobody matched, so this is somebody new.
    """

    candidate_id: uuid.UUID | None = None
    matched_on: str | None = None
    conflict: tuple[uuid.UUID, uuid.UUID] | None = None


async def find_candidate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    email: str | None,
    phone_e164: str | None,
) -> MatchResult:
    """Resolve these details to an existing candidate, if there is one.

    Runs on the caller's session so the lookup and whatever it decides to write
    share one transaction. A separate connection would let a candidate be
    created against a match that was rolled back.

    Isolation is defense in depth here, not a single mechanism: RLS on the
    `candidates` table (keyed off the `app.tenant_id` session setting that
    `tenant_session` establishes) is what actually enforces the boundary, and
    the explicit `tenant_id = :tenant_id` predicate below is a second,
    independent guard against a session that was never scoped — it fails
    closed (no session GUC set means the RLS predicate is NULL and no rows
    return), so a caller that forgets to scope the session gets zero matches
    rather than another tenant's row.
    """
    normalized_email = normalize_email(email)
    # A fixed line identifies a company. Kept on the record, never used to
    # decide that two rows are the same person.
    usable_phone = phone_e164 if is_matchable_phone(phone_e164) else None

    by_email: uuid.UUID | None = None
    by_phone: uuid.UUID | None = None

    if normalized_email:
        row = (
            await session.execute(
                _BY_EMAIL, {"tenant_id": tenant_id, "email": normalized_email}
            )
        ).first()
        by_email = row.id if row else None

    if usable_phone:
        row = (
            await session.execute(
                _BY_PHONE, {"tenant_id": tenant_id, "phone": usable_phone}
            )
        ).first()
        by_phone = row.id if row else None

    # Two keys pointing at two different people. Picking either would attach
    # one person's details to the other's record; creating a third would
    # silently duplicate both. Neither is a decision code should make.
    if by_email and by_phone and by_email != by_phone:
        return MatchResult(conflict=(by_email, by_phone))

    if by_email:
        return MatchResult(candidate_id=by_email, matched_on="email")
    if by_phone:
        return MatchResult(candidate_id=by_phone, matched_on="phone")
    return MatchResult()
