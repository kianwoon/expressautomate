"""users composite key

Revision ID: 9ba7a1519c07
Revises: 8c7e0f3c5305
Create Date: 2026-07-30 16:27:41.942931+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '9ba7a1519c07'
down_revision: str | None = '8c7e0f3c5305'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_users_tenant_id_id", "users", ["tenant_id", "id"])


def downgrade() -> None:
    op.drop_constraint("uq_users_tenant_id_id", "users", type_="unique")
