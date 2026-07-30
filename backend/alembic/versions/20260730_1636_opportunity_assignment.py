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


# Backfill the client from the evidence already recorded per message, but only
# where that evidence points one way.
#
# `client_mentions` is an evidence trail, not a key: it records EVERY client a
# message referred to, and its unique constraint is
# (tenant_id, client_id, email_message_id), so one email legitimately names
# many clients. Production bears this out -- most matched messages mention six
# distinct clients, and narrowing to matched_by = 'email_domain' does not
# resolve it. A plain UPDATE ... FROM would silently pick one of them at
# random, and `client_id` decides who the job order is assigned to, so an
# arbitrary pick routes work to the wrong recruiter.
#
# So: assign only when exactly one distinct client is mentioned. Otherwise
# leave NULL, which means "no client recorded" -- a recruiter can set it. A
# confidently wrong client is worse than none (cf. CLAUDE.md sec 15: never
# fabricate a missing value).
#
# Exposed as a constant so the behaviour can be pinned by a test.
# allow-hardcode: a hand-written one-off data backfill, not a matching oracle.
CLIENT_BACKFILL_SQL = """
    WITH sole_client AS (
        SELECT m.tenant_id,
               m.email_message_id,
               (array_agg(DISTINCT m.client_id))[1] AS client_id
          FROM client_mentions m
         WHERE m.email_message_id IS NOT NULL
         GROUP BY m.tenant_id, m.email_message_id
        HAVING count(DISTINCT m.client_id) = 1
    )
    UPDATE opportunities o
       SET client_id = s.client_id
      FROM sole_client s
     WHERE s.email_message_id = o.email_message_id
       AND s.tenant_id = o.tenant_id
"""


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

    op.execute(CLIENT_BACKFILL_SQL)

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
