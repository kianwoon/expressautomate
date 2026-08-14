"""tenants llm_runs: count analysis RUNS, not rows, for the daily quota

Revision ID: c1v2p0000001
Revises: c1v2n0000001
Create Date: 2026-08-14 15:00:00+00:00

INTELLIGENCE_DAILY_QUOTA was first shipped counting rows created since
midnight — correct for the CV and opportunity-document quotas, whose every
spend is a fresh INSERT, and wrong here: the Intelligence POSTs are upserts
(one row per opportunity/candidate), so a re-run UPDATEs the existing row.
Neither `created_at` (never moves on update) nor a per-row `last_queued_at`
stamp (one row stamped N times is still one row) can count runs — a COUNT()
of rows cannot count events that do not create rows.

This puts the counter where the scope already lives: `tenants`. One date,
one count. The POST's increment is a single atomic UPDATE..RETURNING that
also performs the date rollover, so two concurrent POSTs serialize on the
row lock and neither undercounts. A refused POST has already spent its
increment — harmless: refusals cost no model call and the window resets at
midnight UTC.

Backfill: every tenant starts at 0 for today, so the first deployment of
the quota grants each agency a fresh day.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2p0000001"
down_revision: str | None = "c1v2n0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("llm_runs_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "llm_runs_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "llm_runs_count")
    op.drop_column("tenants", "llm_runs_date")
