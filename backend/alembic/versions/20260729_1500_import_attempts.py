"""candidate import attempt counter

Every other way an import can fail already ends in `failed`: an unreadable
file, the row cap, bytes that are no longer there. A crash *inside*
`apply_import` is the one that does not — the row stays at `parsing`,
`rescan_stuck` finds it, re-enqueues it, and it crashes again. Nothing about
that loop is visible: the sweep logs a requeue, the worker logs a traceback,
and a deterministically bad file burns a worker slot every few minutes for
ever.

The counter is what turns "it failed" into "it failed too many times".
Incremented in the same transaction that moves the row to `parsing`, so a
worker killed without unwinding still spent an attempt — an attempt is a
pickup, not a completion, and counting completions would count zero on
exactly the runs this bounds.

Table-level grants and the tenant policy on `candidate_imports` already cover
every column, so a new column needs neither a GRANT nor a policy change here.

Revision ID: c8e2b47d5a91
Revises: a4d1c6b39e52
Create Date: 2026-07-29 15:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2b47d5a91"
down_revision: str | None = "a4d1c6b39e52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Server default rather than a Python one: `rescan_stuck` re-enqueues rows
    # that already exist, and a NULL here would make the first increment on an
    # older row a no-op — the loop this column exists to stop.
    op.add_column(
        "candidate_imports",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("candidate_imports", "attempts")
