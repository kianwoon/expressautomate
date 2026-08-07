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

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.tenant import User
from app.services.candidate_naming import is_matchable_phone, normalize_email
from app.services.visibility import visible_candidates

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


_CONTACT_LIKE = re.compile(r"@|\d{4,}")
_MAX_TOKENS = 3


def normalize_name(full_name: str) -> str:
    """A name reduced to lowercase alpha tokens for a loose same-person check.

    Not a key — two different people can share a name. This exists only to
    surface a *likely* duplicate for a human to confirm when no matchable
    identity (email/mobile) was found. "Chan  Wen  Jie" and "Chan Wen Jie"
    both reduce to "chan wen jie" here.
    """
    tokens = [w for w in re.split(r"\s+", full_name.lower()) if w.isalpha()]
    return " ".join(tokens)


async def candidates_with_same_name(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    full_name: str,
) -> list[uuid.UUID]:
    """Existing active candidates whose normalized name matches.

    Returns at most a few ids — enough to tell the recruiter "this may be a
    duplicate, confirm before creating". Never used to auto-merge: two real
    people can share a name, and silently merging them is the worst outcome.
    """
    normalized = normalize_name(full_name)
    if not normalized:
        return []
    # Collapse repeated whitespace in the stored name before comparing, so
    # "Chan  Wen  Jie" (double-spaced from PDF extraction) matches "Chan Wen Jie".
    collapsed = func.regexp_replace(Candidate.full_name, r"\s+", " ", "g")
    rows = (
        await session.execute(
            select(Candidate.id)
            .where(Candidate.tenant_id == tenant_id)
            .where(Candidate.record_status == Candidate.ACTIVE)
            .where(func.lower(collapsed) == normalized)
            .limit(5)
        )
    ).scalars().all()
    return list(rows)


def abbreviate(full_name: str) -> str:
    """"Wei Ming Tan" -> "Wei Ming T." — enough to recognise a person you have
    met, not enough to be a directory of who the agency holds.

    The bound is structural, not incidental: at most `_MAX_TOKENS` leading
    tokens are kept (long names are truncated, not spelled out in full), the
    last kept token is initialised, and any token that looks like contact
    data — containing "@" or a run of 4+ digits, i.e. `_CONTACT_LIKE` — is
    replaced with "•" rather than passed through. A name that is entirely
    contact data (a pasted email, a bare phone number) still yields a
    non-empty, non-misleading placeholder instead of leaking verbatim.
    """
    parts = full_name.split()[:_MAX_TOKENS]

    def _clean(token: str) -> str:
        return "•" if _CONTACT_LIKE.search(token) else token

    if not parts:
        return "•"
    if len(parts) == 1:
        token = parts[0]
        if _CONTACT_LIKE.search(token):
            return "•"
        return f"{token[0]}." if len(token) > 1 else token

    cleaned = [_clean(p) for p in parts[:-1]]
    last = parts[-1]
    last_initial = "•" if _CONTACT_LIKE.search(last) else f"{last[0]}."
    return " ".join([*cleaned, last_initial])


async def held_by_colleague(
    session: AsyncSession,
    match: MatchResult,
    user_id: uuid.UUID,
    role: str,
) -> dict[str, object] | None:
    """The 409 body for a candidate that exists but is not ours to see.

    Returns None when the match is visible to the caller — that is the
    ordinary duplicate the route already handled, and the caller can simply be
    sent to the row.

    The disclosure here is deliberate and bounded. The caller learns this
    person is in the agency's database, who holds them, and the row's id —
    and nothing else: no contact detail, no salary, no notes, no client
    history. The alternative is a wall a recruiter cannot act on, and in a
    three-to-fifty person agency they will walk to that colleague's desk and
    ask anyway.

    The id is in the payload because `can_request_access` is otherwise a
    promise the caller cannot keep: `POST /api/candidates/{id}/access-
    requests` is keyed by candidate id, so withholding it leaves a button
    with no endpoint to call. Withholding it bought nothing anyway — this
    body already discloses the two sensitive facts (that the person exists,
    and who holds them), a UUID is unguessable, and every route behind the id
    is guarded: `load_visible_candidate` still 404s it for this caller, and
    `request_candidate_access` 404s an id from another tenant.
    """
    if match.candidate_id is None:
        return None

    visible = (
        await session.execute(
            select(Candidate.id)
            .where(Candidate.id == match.candidate_id)
            .where(visible_candidates(user_id, role))
        )
    ).scalar_one_or_none()
    if visible is not None:
        return None

    masked = await masked_candidate(session, match.candidate_id)
    if masked is None:
        # The row that matched a moment ago was merged or deleted before we
        # could read it. There is nothing to disclose and no colleague to
        # name; fall through to the caller's generic "already recorded" 409.
        return None

    return {
        "reason": "already_registered",
        "candidate": masked,
        "can_request_access": True,
    }


async def masked_candidate(
    session: AsyncSession, candidate_id: uuid.UUID
) -> dict[str, object] | None:
    """The bounded disclosure tier, in one place so it cannot drift.

    `held_by_colleague` above defines it for the 409 collision path, and
    `app/api/sourcing.py` applies the same tier to a shortlist match the
    viewer may not see. Two implementations of "how much may a colleague
    learn" would eventually disagree, and the more generous one would win by
    accident.

    Returns None when the row has gone (merged or deleted between the query
    that named it and this one) — there is then nothing to disclose and no
    colleague to name.
    """
    # Inner join, not outer: a candidate this function is called about is
    # always owned —
    # `visible_candidates` already ruled out the NULL-owner case above (it
    # admits `owner_id IS NULL` for an ordinary recruiter, and is `true_()`
    # for an owner, so either way a NULL-owner row is caught by the `visible
    # is not None` return before this query runs). An outer join here would
    # be a branch that can never execute.
    row = (
        await session.execute(
            select(
                Candidate.full_name,
                # `users` has no `full_name`. Prefer what the person chose to
                # be called, then what the directory says, then the address
                # they signed in with — never a bare UUID, which tells the
                # recruiter nothing and looks like a bug.
                func.coalesce(User.preferred_name, User.display_name, User.email).label(
                    "holder"
                ),
            )
            .join(User, User.id == Candidate.owner_id)
            .where(Candidate.id == candidate_id)
        )
    ).one_or_none()
    if row is None:
        return None

    return {
        "full_name": abbreviate(row.full_name),
        "held_by": row.holder,
        "id": str(candidate_id),
    }


async def patch_collision(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    candidate: Candidate,
    values: dict[str, object],
    user_id: uuid.UUID,
    role: str,
) -> dict[str, object] | None:
    """The 409 body for a PATCH whose new email/phone collides with a row
    outside the caller's visibility. A PATCH that leaves both keys untouched
    matches the row being edited, and that is never a collision.
    """
    if "email" not in values and "phone_e164" not in values:
        return None
    match = await find_candidate(
        session,
        tenant_id,
        values.get("email", candidate.email),
        values.get("phone_e164", candidate.phone_e164),
    )
    if match.candidate_id is None or match.candidate_id == candidate.id:
        return None
    return await held_by_colleague(session, match, user_id, role)
