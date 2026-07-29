"""WA gateway session tables

Creates `wa_sessions` and `wa_session_keys` — the Baileys auth-state store of
the 2026-07-29 gateway plan, §3. See `app/models/wa_session.py` for why the
key store is one row per key rather than one blob per session; the short
version is that Baileys mutates a few signal keys at a time and a whole-blob
write would drop whichever key lost the race, which plan §11 names as the
riskiest failure in the build.

RLS is enabled and forced in this same revision, for the reason every prior
migration gives: `verify_rls_enforced()` refuses to boot on a readable table
without a forced policy, so adding the policy later would be a silent
cross-tenant leak in the gap between "table exists" and "policy exists".

`wa_session_keys.value_encrypted` is AES-256-GCM ciphertext (version byte ‖
12-byte IV ‖ 16-byte tag ‖ ciphertext, AAD `session_id:category:key_id`). The
key is `WA_GATEWAY_ENCRYPTION_KEY`, set on the `gateway` Koyeb service only —
so the app role can read these rows and still cannot read the values. That
asymmetry is intentional and must not be "fixed" by giving FastAPI the key.

`wa_session_keys` deliberately has no CHECK on `category`: the vocabulary is
Baileys' `SignalDataTypeMap`, not ours, and a Baileys upgrade that adds a
category would otherwise turn into a write failure that logs every recruiter
out.

Revision ID: 4b7d2e91c6f8
Revises: 6f1a9c3d0b52
Create Date: 2026-07-29 21:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "4b7d2e91c6f8"
down_revision: str | None = "6f1a9c3d0b52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTING = "app.tenant_id"

_STATUSES = ("pairing", "connected", "reconnecting", "disconnected", "logged_out")


def _enable_rls(table: str) -> None:
    predicate = f"tenant_id = nullif(current_setting('{_SETTING}', true), '')::uuid"
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


def upgrade() -> None:
    op.create_table(
        "wa_sessions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_e164", sa.String(20)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pairing"),
        sa.Column("status_detail", sa.Text()),
        sa.Column("qr_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_connected_at", sa.DateTime(timezone=True)),
        sa.Column("sent_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_date", sa.Date()),
        sa.Column("ban_risk_acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # Globally unique, not per-tenant: one WhatsApp device pairs to one
        # process, and the gateway's advisory lock (plan §2) is keyed on
        # user_id. Two rows for one user would let two sockets fight over the
        # same device and get both logged out.
        sa.UniqueConstraint("user_id", name="uq_wa_sessions_user_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_wa_sessions_tenant_id_id"),
        sa.CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in _STATUSES) + ")",
            name="ck_wa_sessions_status",
        ),
    )
    op.create_index("ix_wa_sessions_tenant_id", "wa_sessions", ["tenant_id"])

    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON wa_sessions TO "{settings.DATABASE_APP_ROLE}"'
    )
    _enable_rls("wa_sessions")

    op.create_table(
        "wa_session_keys",
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(48), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("value_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        # The address Baileys asks for IS the primary key, so a write is an
        # upsert of exactly one row and cannot disturb a key it never named.
        sa.PrimaryKeyConstraint(
            "session_id", "category", "key_id", name="pk_wa_session_keys"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["wa_sessions.tenant_id", "wa_sessions.id"],
            name="fk_wa_session_keys_session_same_tenant",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_wa_session_keys_tenant_id", "wa_session_keys", ["tenant_id"])
    op.create_index("ix_wa_session_keys_session_id", "wa_session_keys", ["session_id"])

    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON wa_session_keys '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )
    _enable_rls("wa_session_keys")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON wa_session_keys")
    op.drop_table("wa_session_keys")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON wa_sessions")
    op.drop_table("wa_sessions")
