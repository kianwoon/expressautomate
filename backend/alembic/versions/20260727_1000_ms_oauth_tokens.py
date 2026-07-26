"""ms oauth tokens

Stored Microsoft refresh tokens, tenant-scoped and behind the same forced RLS
policy as every other business table (plan §18, §30). The token column holds
Fernet ciphertext, never a bearer secret in clear.

Revision ID: c4a1d7f9b210
Revises: 8bbbf30a6292
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a1d7f9b210"
down_revision: str | None = "8bbbf30a6292"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ms_oauth_tokens"
SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("home_account_id", sa.String(length=128), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(length=1024), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_ms_tokens_tenant_user"),
    )
    op.create_index(op.f("ix_ms_oauth_tokens_tenant_id"), TABLE, ["tenant_id"], unique=False)
    op.create_index(op.f("ix_ms_oauth_tokens_user_id"), TABLE, ["user_id"], unique=False)

    # Same shape as the tenants/users policies in 20260726_1800. NULLIF turns
    # the '' an unscoped session reads back into NULL, so the predicate matches
    # nothing rather than raising on the uuid cast.
    predicate = f"tenant_id = nullif(current_setting('{SETTING}', true), '')::uuid"
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_index(op.f("ix_ms_oauth_tokens_user_id"), table_name=TABLE)
    op.drop_index(op.f("ix_ms_oauth_tokens_tenant_id"), table_name=TABLE)
    op.drop_table(TABLE)
