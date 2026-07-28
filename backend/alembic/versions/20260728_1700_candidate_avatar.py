"""candidate avatar

Revision ID: 1519048c9751
Revises: a2d71b8c4f39
Create Date: 2026-07-28 17:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '1519048c9751'
down_revision: str | None = 'a2d71b8c4f39'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('candidates', sa.Column('avatar_key', sa.Text(), nullable=True))
    op.add_column(
        'candidates',
        sa.Column('avatar_updated_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('candidates', 'avatar_updated_at')
    op.drop_column('candidates', 'avatar_key')
