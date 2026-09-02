"""drop job_intelligence.allow_thin: the thin-order refusal is gone

Revision ID: c1v2s0000001
Revises: c1v2r0000001
Create Date: 2026-09-02 13:00:00+00:00

`allow_thin` existed so a recruiter could escape the thin-order pre-flight
refusal with an explicit "Run anyway" click. The refusal itself is now gone:
the understand prompt's two-tier grounding (order-specific facts stay
grounded; the title's typical meaning is fair game, phrased as typical and
scored with low confidence) makes a title-only order produce a useful
analysis, so a guard that refuses one before spending a model call no longer
earns its place — and neither does the override flag. Every run analyses;
the one-click contract is "Run analysis always runs".

The column shipped hours ago and carries only defaults (false everywhere —
the escape hatch was clicked once, in production, on the row that proved the
point), so dropping it loses nothing.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1v2s0000001"
down_revision: str | None = "c1v2r0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("job_intelligence", "allow_thin")


def downgrade() -> None:
    import sqlalchemy as sa

    op.add_column(
        "job_intelligence",
        sa.Column("allow_thin", sa.Boolean(), nullable=False, server_default="false"),
    )
