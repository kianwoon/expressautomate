"""client assignee column-qualified SET NULL

Revision ID: a1b2c3d4e5f6
Revises: f3cc4b20b322
Create Date: 2026-07-31 09:00:00.000000+00:00
"""
from collections.abc import Sequence

from alembic import op


revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = 'f3cc4b20b322'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A plain SET NULL on a composite FK nulls every referencing column,
    # including `tenant_id`, which is NOT NULL — deleting a recruiter with
    # any assigned client fails outright. Column-qualified SET NULL (PG15+)
    # clears only `assigned_user_id`, same fix as
    # `fk_opportunities_assignee_same_tenant` in f3cc4b20b322.
    op.drop_constraint("fk_clients_assignee_same_tenant", "clients", type_="foreignkey")
    op.create_foreign_key(
        "fk_clients_assignee_same_tenant",
        "clients",
        "users",
        ["tenant_id", "assigned_user_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL (assigned_user_id)",
    )


def downgrade() -> None:
    op.drop_constraint("fk_clients_assignee_same_tenant", "clients", type_="foreignkey")
    op.create_foreign_key(
        "fk_clients_assignee_same_tenant",
        "clients",
        "users",
        ["tenant_id", "assigned_user_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL",
    )
