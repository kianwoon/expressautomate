"""wa gateway P5: a pending row before dispatch, and persisted send spacing

Two closed races (P5 carry-forwards from the P4 review):

1. **The daily cap can be raced.** Two concurrent sends both read "49 sent
   today" and both proceed. Fixed by writing the activity row *before*
   dispatch, as `'pending'`, inside the same transaction that counts against
   the cap — so the count a second concurrent request sees already includes
   the first request's in-flight send. `pending` is therefore a real status a
   reader may see, not a transient value that only ever exists mid-request:
   we are trying, and we do not yet know. It resolves to `sent`, `failed` or
   `unknown` once the gateway answers (or the liveness sweep gives up on it).

2. **The idempotency key has an in-flight gap.** A replay of a
   `client_request_id` arriving while the first send is still out used to find
   no row at all and dispatch a second message. With the row written before
   dispatch, the replay finds the `pending` row and returns it unchanged —
   dispatching nothing.

Both races close the same way: `INSERT ... ON CONFLICT (tenant_id,
client_request_id) DO NOTHING` inside a transaction that has already taken a
row lock on `wa_sessions` (`SELECT ... FOR UPDATE`), which also serialises
concurrent sends *from the same session* so the cap check and the spacing
check are both read-then-act against a consistent snapshot.

Send spacing (plan §9) moves here for the same reason: the review that asked
for the pending row also asked that the jittered deadline be persisted rather
than re-rolled per refusal, because a recruiter who waits exactly the
`retry_after_seconds` we quoted must succeed on retry — re-rolling would make
that quote a lie (§15's shape, applied to a promise about the future rather
than a claim about the past). `wa_sessions.next_send_allowed_at` is written
once, when a send is admitted, and read (never rewritten) by every refusal
until the next admitted send moves it.

Revision ID: c3f8a2d5e194
Revises: 9a4e6c81b3d7
Create Date: 2026-07-29 23:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f8a2d5e194"
down_revision: str | None = "9a4e6c81b3d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors `CandidateActivity.STATUSES` in app/models/candidate.py — written
# out rather than imported, same reasoning as the revisions before this one.
_STATUSES = ("opened", "sent", "failed", "unknown", "pending")
_OLD_STATUSES = ("opened", "sent", "failed", "unknown")

_STATUS_CHECK = "ck_candidate_activities_status_known"


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.drop_constraint(_STATUS_CHECK, "candidate_activities", type_="check")
    op.create_check_constraint(
        _STATUS_CHECK, "candidate_activities", f"status IN ({_in_list(_STATUSES)})"
    )
    op.add_column(
        "wa_sessions",
        # The jittered deadline the *last admitted send* set, read by every
        # subsequent refusal and never rewritten until the next admitted
        # send. NULL means no send has ever been admitted on this session, so
        # the very first send always clears the spacing check.
        sa.Column("next_send_allowed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wa_sessions", "next_send_allowed_at")
    # Rows written in the widened vocabulary would fail the narrowed CHECK.
    # A `pending` row that survives to here means a dispatch that never
    # resolved — deleting it loses no evidence a `sent`/`failed`/`unknown`
    # row would have, because a `pending` row has none yet (§15).
    op.execute("DELETE FROM candidate_activities WHERE status = 'pending'")
    op.drop_constraint(_STATUS_CHECK, "candidate_activities", type_="check")
    op.create_check_constraint(
        _STATUS_CHECK, "candidate_activities", f"status IN ({_in_list(_OLD_STATUSES)})"
    )
