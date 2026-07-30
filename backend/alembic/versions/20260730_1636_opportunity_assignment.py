"""opportunity assignment

Revision ID: f3cc4b20b322
Revises: 8bff2331cd50
Create Date: 2026-07-30 16:36:30.623002+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'f3cc4b20b322'
down_revision: str | None = '8bff2331cd50'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # First, so `opportunity_shares` can declare a composite FK against it in
    # the next migration. Postgres refuses a composite FK without a matching
    # unique constraint on the referenced columns.
    op.create_unique_constraint(
        "uq_opportunities_tenant_id_id", "opportunities", ["tenant_id", "id"]
    )
    op.add_column("opportunities", sa.Column("client_id", sa.UUID(), nullable=True))
    op.add_column("opportunities", sa.Column("assigned_user_id", sa.UUID(), nullable=True))
    # NOT NULL on a table with existing rows, so it needs a server default at
    # add-time — the same reason `clients.source` carries one.
    op.add_column(
        "opportunities",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="pipeline"),
    )
    op.create_index(op.f("ix_opportunities_client_id"), "opportunities", ["client_id"])
    op.create_index(
        op.f("ix_opportunities_assigned_user_id"), "opportunities", ["assigned_user_id"]
    )
    op.create_check_constraint(
        "ck_opportunities_source_known", "opportunities", "source IN ('pipeline', 'manual')"
    )
    # Column-qualified SET NULL (PG15+): a plain SET NULL on a composite FK
    # nulls every referencing column, including `tenant_id`, which is
    # NOT NULL and would fail the delete. Only the assignment column clears.
    op.create_foreign_key(
        "fk_opportunities_client_same_tenant",
        "opportunities",
        "clients",
        ["tenant_id", "client_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL (client_id)",
    )
    op.create_foreign_key(
        "fk_opportunities_assignee_same_tenant",
        "opportunities",
        "users",
        ["tenant_id", "assigned_user_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL (assigned_user_id)",
    )

    # Backfill the client from the evidence already recorded per message.
    # Rows whose mention is gone stay NULL, which is honest: the link existed
    # and the record of it does not.
    op.execute(
        """
        UPDATE opportunities o
           SET client_id = m.client_id
          FROM client_mentions m
         WHERE m.email_message_id = o.email_message_id
           AND m.tenant_id = o.tenant_id
        """
    )

    # email_message_id: NOT NULL -> nullable, CASCADE -> SET NULL.
    op.alter_column("opportunities", "email_message_id", nullable=True)
    op.drop_constraint(
        "opportunities_email_message_id_fkey", "opportunities", type_="foreignkey"
    )
    op.create_foreign_key(
        "opportunities_email_message_id_fkey",
        "opportunities",
        "email_messages",
        ["email_message_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "opportunities_email_message_id_fkey", "opportunities", type_="foreignkey"
    )
    op.create_foreign_key(
        "opportunities_email_message_id_fkey",
        "opportunities",
        "email_messages",
        ["email_message_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute("DELETE FROM opportunities WHERE email_message_id IS NULL")
    op.alter_column("opportunities", "email_message_id", nullable=False)
    op.drop_constraint(
        "fk_opportunities_assignee_same_tenant", "opportunities", type_="foreignkey"
    )
    op.drop_constraint("fk_opportunities_client_same_tenant", "opportunities", type_="foreignkey")
    op.drop_constraint("ck_opportunities_source_known", "opportunities", type_="check")
    op.drop_column("opportunities", "source")
    op.drop_column("opportunities", "assigned_user_id")
    op.drop_column("opportunities", "client_id")
    op.drop_constraint("uq_opportunities_tenant_id_id", "opportunities", type_="unique")
