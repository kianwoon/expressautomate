"""trusted sender domains skip the relevance gate

A recruiter's job orders arrive mostly from a handful of client domains, and
the gate is the highest-volume LLM call in the system. Once the gate has
confidently answered `recruitment` for a sender's domain N times, asking it
again for the next email from that domain buys nothing — the answer is known
in advance.

This migration adds `trusted_senders`, one row per (tenant, sender domain).
The table is self-seeding: the classify jobs upsert a domain after a confident
`recruitment` verdict, and consult it before the LLM call. An email from a
trusted domain is marked `recruitment` directly — `classification_model` is
set to `trusted-sender` so a report can tell the free answers from the paid
ones — and proceeds to extraction exactly as a gated verdict would.

The skip is deliberately coarse (domain, not address) and deliberately
fail-open: a missing row simply means the gate runs. Nothing about the skip
can drop a job order, because the fallback is the same gate that ran before
this table existed.

RLS is FORCEd like every tenant table.

Revision ID: c1v2h0000001
Revises: c1v2g0000001
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2h0000001"
down_revision: str | None = "c1v2g0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "trusted_senders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # Lowercased mail domain, exactly as `client_naming.domain_of` emits it.
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One trust row per domain per tenant: the gate either knows a domain
        # or it does not, and repeated upserts must converge on one row.
        sa.UniqueConstraint("tenant_id", "domain", name="uq_trusted_senders_domain"),
    )
    op.create_index(
        op.f("ix_trusted_senders_tenant_id"),
        "trusted_senders",
        ["tenant_id"],
        unique=False,
    )

    _enforce_rls()


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON trusted_senders")
    op.execute("ALTER TABLE trusted_senders DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE trusted_senders NO FORCE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_trusted_senders_tenant_id"), table_name="trusted_senders")
    op.drop_table("trusted_senders")


def _enforce_rls() -> None:
    """FORCE, not merely ENABLE — see `20260727_1900_extraction_tables.py`."""
    table = "trusted_senders"
    column = "tenant_id"
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
