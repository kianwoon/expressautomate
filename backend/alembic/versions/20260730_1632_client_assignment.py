"""client assignment

Revision ID: 8bff2331cd50
Revises: 9ba7a1519c07
Create Date: 2026-07-30 16:32:02.856677+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '8bff2331cd50'
down_revision: str | None = '9ba7a1519c07'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SETTING = "app.tenant_id"
PROTECTED = (("client_collaborators", "tenant_id"),)


def upgrade() -> None:
    op.add_column("clients", sa.Column("assigned_user_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_clients_assignee_same_tenant",
        "clients",
        "users",
        ["tenant_id", "assigned_user_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "client_collaborators",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_client_collaborators_client_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_client_collaborators_user_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "client_id", "user_id", name="uq_client_collaborators_once"
        ),
    )
    op.create_index(
        op.f("ix_client_collaborators_client_id"), "client_collaborators", ["client_id"]
    )
    op.create_index(op.f("ix_client_collaborators_user_id"), "client_collaborators", ["user_id"])
    op.create_index(
        op.f("ix_client_collaborators_tenant_id"), "client_collaborators", ["tenant_id"]
    )

    _enforce_rls()


def _enforce_rls() -> None:
    """The same policy every tenant-scoped table carries, for the same reasons.

    FORCE, not merely ENABLE: without it the table owner bypasses the policy,
    and the owner is who migrations and any superuser session connect as.
    """
    for table, column in PROTECTED:
        predicate = f"{column} = nullif(current_setting('{SETTING}', true), '')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )


def downgrade() -> None:
    op.drop_table("client_collaborators")
    op.drop_constraint("fk_clients_assignee_same_tenant", "clients", type_="foreignkey")
    op.drop_column("clients", "assigned_user_id")
