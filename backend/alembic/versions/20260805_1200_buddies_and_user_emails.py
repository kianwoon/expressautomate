"""buddies and user emails

Revision ID: c1a0d5e7b210
Revises: c1a0d5e7b209
Create Date: 2026-08-05 12:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "c1a0d5e7b210"
down_revision: str | None = "c1a0d5e7b209"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "app.tenant_id"
PROTECTED = [
    ("user_emails", "tenant_id"),
    ("buddies", "tenant_id"),
    ("buddy_referrals", "tenant_id"),
]


def upgrade() -> None:
    # --- user_emails ---
    op.create_table(
        "user_emails",
        sa.Column("user_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_emails_user_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_user_emails_tenant_email"),
    )
    op.create_index(
        op.f("ix_user_emails_tenant_id"), "user_emails", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_user_emails_user_id"), "user_emails", ["user_id"]
    )

    # --- buddies ---
    op.create_table(
        "buddies",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_domain", sa.String(length=255), nullable=True),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="pipeline",
        ),
        sa.Column("id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_buddies_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_buddies_tenant_email"),
    )
    op.create_index(
        op.f("ix_buddies_tenant_id"), "buddies", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_buddies_email_domain"), "buddies", ["email_domain"]
    )

    # --- buddy_referrals ---
    op.create_table(
        "buddy_referrals",
        sa.Column("buddy_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("client_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column(
            "email_message_id", PgUUID(as_uuid=True), nullable=True
        ),
        sa.Column("id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "buddy_id"],
            ["buddies.tenant_id", "buddies.id"],
            name="fk_buddy_referrals_buddy_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_buddy_referrals_client_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["email_message_id"],
            ["email_messages.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "buddy_id", "client_id", name="uq_buddy_referrals_once"
        ),
    )
    op.create_index(
        op.f("ix_buddy_referrals_tenant_id"), "buddy_referrals", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_buddy_referrals_buddy_id"), "buddy_referrals", ["buddy_id"]
    )
    op.create_index(
        op.f("ix_buddy_referrals_client_id"), "buddy_referrals", ["client_id"]
    )
    op.create_index(
        op.f("ix_buddy_referrals_email_message_id"),
        "buddy_referrals",
        ["email_message_id"],
    )

    _enforce_rls()


def _enforce_rls() -> None:
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
    op.drop_table("buddy_referrals")
    op.drop_table("buddies")
    op.drop_table("user_emails")
