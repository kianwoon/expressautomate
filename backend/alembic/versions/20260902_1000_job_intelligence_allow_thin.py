"""job_intelligence.allow_thin: the recruiter's "Run anyway" on a thin order

Revision ID: c1v2r0000001
Revises: c1v2q0000001
Create Date: 2026-09-02 10:00:00+00:00

A title-only job order fails before any LLM call with an actionable sentence
(is_thin pre-flight, 2026-09-01). The sentence now carries a "Run anyway"
escape hatch: the recruiter's choice persists as this column on the row, so
the worker skips its pre-flight and the understand prompt answers thinly at
low confidence — the exact case that prompt was already hardened for.

A column rather than job-payload state because `rescan_stuck` re-enqueues
stranded rows with a bare (tenant, row) pair and would otherwise strip the
recruiter's choice mid-flight.

Backfill: every existing row predates the flag, and all of them ran (or
failed) under the old gate — false is both the server default and the honest
history.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2r0000001"
down_revision: str | None = "c1v2q0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_intelligence",
        sa.Column("allow_thin", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("job_intelligence", "allow_thin")
