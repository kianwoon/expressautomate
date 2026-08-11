"""classification usage telemetry

The relevance gate is the highest-volume LLM call in the system and, until
now, the only one with no cost provenance: `extractions` records prompt and
completion tokens per extraction, but nothing records what the gate spent per
email. A cost plan needs a before/after; without gate telemetry every saving
is measured against a guess.

This migration adds `classification_usages`, one row per gate verdict
(single-email and batched alike). It deliberately mirrors the columns of
`extractions` — prompt_tokens, completion_tokens, latency_ms, model_name,
prompt_version — so a per-email cost report can join the two tables with one
vocabulary. The table is written by the gate jobs and read by nobody yet; the
point of shipping it is that the data exists from the moment the feature
lands, not after someone remembers to add it.

RLS is enabled and FORCEd exactly like every tenant table, and the
`tenant_isolation` policy is created here rather than left to
`verify_rls_enforced()` to fail startup over.

Revision ID: c1v2g0000001
Revises: c1v2f0000001
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2g0000001"
down_revision: str | None = "c1v2f0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "classification_usages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email_message_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        # Mirrors `extractions`: the model that actually answered (OpenRouter
        # may route elsewhere than asked), the prompt version under which it
        # answered, and the token counts that turn a report into a bill.
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["email_message_id"],
            ["email_messages.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_classification_usages_tenant_id"),
        "classification_usages",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_classification_usages_email_message_id"),
        "classification_usages",
        ["email_message_id"],
        unique=False,
    )

    _enforce_rls()


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON classification_usages")
    op.execute("ALTER TABLE classification_usages DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE classification_usages NO FORCE ROW LEVEL SECURITY")
    op.drop_index(
        op.f("ix_classification_usages_email_message_id"),
        table_name="classification_usages",
    )
    op.drop_index(
        op.f("ix_classification_usages_tenant_id"),
        table_name="classification_usages",
    )
    op.drop_table("classification_usages")


def _enforce_rls() -> None:
    """FORCE, not merely ENABLE — see `20260727_1900_extraction_tables.py`."""
    table = "classification_usages"
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
