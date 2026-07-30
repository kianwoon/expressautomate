"""client logo

Revision ID: 8c7e0f3c5305
Revises: a0bfc93f7eb8
Create Date: 2026-07-30 10:06:31.281015+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '8c7e0f3c5305'
down_revision: str | None = 'a0bfc93f7eb8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('clients', sa.Column('logo_key', sa.Text(), nullable=True))
    op.add_column('clients', sa.Column('logo_updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('clients', 'logo_updated_at')
    op.drop_column('clients', 'logo_key')
