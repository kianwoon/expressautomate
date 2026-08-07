"""candidate intelligence v2 columns

Revision ID: c1v2a0000001
Revises: 4989e08d79a5
Create Date: 2026-08-07 14:00:00+00:00

Clean break from the v1 three-stage skill extractor to the v2 residual-value
engine. Drops the `career` / `capability` / `profile` JSONB columns and adds
`history` / `automation` / `benchmark` / `gaps` / `residual` — one per pipeline
stage of the v2 design.

Existing analyses become state-only rows with null payloads: the serializer
returns `intelligence: null` until the row is re-run, which is the correct
"re-run to get v2 analysis" signal. No data is backfilled — the v1 payloads are
a different shape and carry none of the v2 conclusions (automation exposure,
market benchmark, gap analysis, residual value), so preserving them would be
misleading.

The state machine, RLS policy, trigger, unique constraint, FK and the
`stalled_candidate_intelligence` resolver are all unchanged — only the result
columns change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c1v2a0000001"
down_revision: str | None = "4989e08d79a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the v1 columns. Existing rows lose their payloads — the clean break.
    op.drop_column("candidate_intelligence", "career")
    op.drop_column("candidate_intelligence", "capability")
    op.drop_column("candidate_intelligence", "profile")

    # Add the v2 columns, one per pipeline stage. All nullable: a row that is
    # `pending`, `running`, or `failed` carries no analysis.
    op.add_column(
        "candidate_intelligence",
        sa.Column("history", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "candidate_intelligence",
        sa.Column("automation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "candidate_intelligence",
        sa.Column("benchmark", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "candidate_intelligence",
        sa.Column("gaps", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "candidate_intelligence",
        sa.Column("residual", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidate_intelligence", "residual")
    op.drop_column("candidate_intelligence", "gaps")
    op.drop_column("candidate_intelligence", "benchmark")
    op.drop_column("candidate_intelligence", "automation")
    op.drop_column("candidate_intelligence", "history")
    op.add_column(
        "candidate_intelligence",
        sa.Column("career", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "candidate_intelligence",
        sa.Column("capability", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "candidate_intelligence",
        sa.Column("profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
