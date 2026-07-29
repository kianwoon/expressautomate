"""candidate activities: the WA gateway can now observe a send

Step 1 of WhatsApp outreach could only ever record `'opened'`, because the
recruiter pressed send inside WhatsApp Web and nothing here watched it happen
(`20260729_1900_candidate_activities.py` says exactly that, and it was true
the day it ran). The WA gateway of the 2026-07-29 Baileys plan changes the
fact, not the honesty rule: this process now holds the socket that hands the
message to WhatsApp, so there is finally something to observe.

§15 honesty, stated here and in `CandidateActivity`'s docstring (plan §8).
Four values, and the fourth is the one that keeps the other three true:

- `sent` — the gateway's socket accepted the message and WhatsApp returned a
  provider message id. It does not mean delivered, and never means read.
- `failed` — the gateway **explicitly refused**, and `error` holds its own
  message verbatim. Never a paraphrase and never an inference.
- `unknown` — we dispatched and never learned the outcome: the connection
  dropped or timed out *after* the request went out, so WhatsApp may well
  have accepted it. Writing `failed` here would be a claim we cannot support,
  and a recruiter acting on it would send the same message twice.
- `opened` — unchanged, the popup path. No existing row is backfilled or
  reinterpreted.

Delivery receipts are deliberately not ingested in v1, so no `delivered` or
`read` value exists in this vocabulary; adding one requires actually observing
`messages.update` from Baileys, its own migration, and nothing else may write
them.

`client_request_id` is the caller's own idempotency key. Unique **per tenant**
rather than globally, because it is a value the browser generates: two tenants
colliding on one is a coincidence, not a replay, and a global unique index
would let agency A's key make agency B's send fail (§18). Nullable because
every row written before this revision has none, and because the popup path
(`whatsapp_opened`) has nothing to be idempotent about — Postgres does not
treat NULLs as equal, so any number of rows may carry none.

The three CHECK constraints are dropped and recreated by their existing names
rather than renamed, so `downgrade()` restores the original vocabulary
exactly. RLS, its policy and the grants stay as `5c2d8e14f6a1` created them —
this revision only widens CHECKs and adds columns, so recreating them would
be a second definition of the same thing to drift from.

Revision ID: 9a4e6c81b3d7
Revises: 4b7d2e91c6f8
Create Date: 2026-07-29 22:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "9a4e6c81b3d7"
down_revision: str | None = "4b7d2e91c6f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors `CandidateActivity` in app/models/candidate.py — written out rather
# than imported, same reasoning as the revision that created the table: this
# revision must keep meaning what it meant the day it ran.
_ACTIVITY_TYPES = ("whatsapp_opened", "whatsapp_sent")
_CHANNELS = ("whatsapp",)
_STATUSES = ("opened", "sent", "failed", "unknown")

# What `5c2d8e14f6a1` left behind, restored verbatim by `downgrade()`.
_OLD_ACTIVITY_TYPES = ("whatsapp_opened",)
_OLD_STATUSES = ("opened",)

_TYPE_CHECK = "ck_candidate_activities_type_known"
_CHANNEL_CHECK = "ck_candidate_activities_channel_known"
_STATUS_CHECK = "ck_candidate_activities_status_known"
_CLIENT_REQUEST_UNIQUE = "uq_candidate_activities_tenant_client_request"


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _reset_checks(activity_types: tuple[str, ...], statuses: tuple[str, ...]) -> None:
    """Drop and recreate all three by name, in one place.

    `channel` is unchanged by this revision and is still redefined here: the
    three constraints are one vocabulary, and a helper that redefined only
    two would be the kind of asymmetry a later edit trips over.
    """
    for name in (_TYPE_CHECK, _CHANNEL_CHECK, _STATUS_CHECK):
        op.drop_constraint(name, "candidate_activities", type_="check")
    op.create_check_constraint(
        _TYPE_CHECK, "candidate_activities", f"activity_type IN ({_in_list(activity_types)})"
    )
    op.create_check_constraint(
        _CHANNEL_CHECK, "candidate_activities", f"channel IN ({_in_list(_CHANNELS)})"
    )
    op.create_check_constraint(
        _STATUS_CHECK, "candidate_activities", f"status IN ({_in_list(statuses)})"
    )


def upgrade() -> None:
    op.add_column(
        "candidate_activities",
        # WhatsApp's own id for the message, returned by the gateway's send
        # call. Kept so a support question ("did this go out?") can be
        # answered against WhatsApp rather than against our own assertion.
        sa.Column("provider_message_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "candidate_activities",
        # The gateway's refusal, verbatim. Never a URL and never the shared
        # secret — `app/api/candidates.py` decides a message is safe to store
        # before it reaches this column.
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.add_column(
        "candidate_activities",
        sa.Column("client_request_id", PgUUID(as_uuid=True), nullable=True),
    )
    # Per tenant, not global — see the module docstring. This index is also
    # what actually enforces idempotency: the endpoint's read-then-write is
    # racy under two simultaneous clicks, and the loser of that race gets an
    # IntegrityError here and re-reads the winner's row.
    op.create_index(
        _CLIENT_REQUEST_UNIQUE,
        "candidate_activities",
        ["tenant_id", "client_request_id"],
        unique=True,
    )
    _reset_checks(_ACTIVITY_TYPES, _STATUSES)


def downgrade() -> None:
    # Rows written by the widened vocabulary would fail the narrowed CHECK, so
    # they go first. Deleting them is right rather than harsh: downgrading past
    # this revision also drops the columns that carry the only evidence a
    # `sent`/`failed`/`unknown` row had, and a row surviving with its evidence
    # removed would be a claim about a send with nothing behind it (§15).
    op.execute(
        "DELETE FROM candidate_activities "
        "WHERE status IN ('sent', 'failed', 'unknown') "
        "OR activity_type = 'whatsapp_sent'"
    )
    _reset_checks(_OLD_ACTIVITY_TYPES, _OLD_STATUSES)
    op.drop_index(_CLIENT_REQUEST_UNIQUE, table_name="candidate_activities")
    op.drop_column("candidate_activities", "client_request_id")
    op.drop_column("candidate_activities", "error")
    op.drop_column("candidate_activities", "provider_message_id")
