"""candidate last-drawn salary columns

Revision ID: c1v2c0000001
Revises: c1v2b0000001
Create Date: 2026-08-08 10:00:00+00:00

Adds a salary trio for what a candidate last/currently earned, quoted from the
CV at parse time. Parallel to the existing `expected_salary` / `salary_currency`
/ `salary_period` trio but populated by extraction (fill-if-NULL) rather than
only by a recruiter. Its own currency and period columns because a candidate
can state current and expected in different units.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2c0000001"
down_revision: str | None = "c1v2b0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column("last_drawn_salary", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "candidates",
        sa.Column("last_drawn_currency", sa.String(8), nullable=True),
    )
    op.add_column(
        "candidates",
        sa.Column("last_drawn_period", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidates", "last_drawn_period")
    op.drop_column("candidates", "last_drawn_currency")
    op.drop_column("candidates", "last_drawn_salary")
