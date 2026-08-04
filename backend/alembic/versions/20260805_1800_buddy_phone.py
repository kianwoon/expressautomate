"""buddy phone column

Revision ID: c1a0d5e7b211
Revises: c1a0d5e7b210
Create Date: 2026-08-05 18:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c1a0d5e7b211"
down_revision: str | None = "c1a0d5e7b210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("buddies", sa.Column("phone", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("buddies", "phone")
