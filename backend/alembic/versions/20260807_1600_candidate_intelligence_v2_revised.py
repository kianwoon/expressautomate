"""candidate intelligence v2 revised (2-stage)

Revision ID: c1v2b0000001
Revises: c1v2a0000001
Create Date: 2026-08-07 16:00:00+00:00

Rebuilds the candidate intelligence from the 5-stage model (history/
automation/benchmark/gaps/residual) into the 2-stage sharp model (work +
assessment) per the revised design doc. Drops the 5 old JSONB columns, adds
`work` and `assessment`. Clean break — existing rows become null-payload until
re-run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c1v2b0000001"
down_revision: str | None = "c1v2a0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("candidate_intelligence", "history")
    op.drop_column("candidate_intelligence", "automation")
    op.drop_column("candidate_intelligence", "benchmark")
    op.drop_column("candidate_intelligence", "gaps")
    op.drop_column("candidate_intelligence", "residual")

    op.add_column(
        "candidate_intelligence",
        sa.Column("work", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "candidate_intelligence",
        sa.Column(
            "assessment", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("candidate_intelligence", "assessment")
    op.drop_column("candidate_intelligence", "work")
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
