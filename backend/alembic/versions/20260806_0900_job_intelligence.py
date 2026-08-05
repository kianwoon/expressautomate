"""job intelligence table

Revision ID: c1a0d5e7b216
Revises: c1a0d5e7b215
Create Date: 2026-08-06 09:00:00+00:00

Adds `job_intelligence`, one row per opportunity holding the latest three-stage
Job Intelligence analysis (understanding / persona / search plan). Re-running
the analysis upserts on `(tenant_id, opportunity_id)` so the row is an UPDATE,
not a second insert.

RLS-protected like every other tenant-scoped table: `verify_rls_enforced()`
refuses to start the app if a `tenant_id` table has no policy, so this table
must be in the `PROTECTED` list or the deployment will not boot.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1a0d5e7b216"
down_revision: str | None = "c1a0d5e7b215"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# See `20260727_1900_extraction_tables.py` for the RLS-enforcement pattern.
PROTECTED: list[tuple[str, str]] = [("job_intelligence", "tenant_id")]
SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "job_intelligence",
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column("understanding", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("persona", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("search_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("removed_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("analysed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "opportunity_id",
            name="uq_job_intelligence_one_per_opportunity",
        ),
    )
    op.create_index(
        op.f("ix_job_intelligence_opportunity_id"),
        "job_intelligence",
        ["opportunity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_intelligence_tenant_id"),
        "job_intelligence",
        ["tenant_id"],
        unique=False,
    )

    _enforce_rls()
    _touch_updated_at()


def downgrade() -> None:
    op.drop_index(op.f("ix_job_intelligence_tenant_id"), table_name="job_intelligence")
    op.drop_index(
        op.f("ix_job_intelligence_opportunity_id"), table_name="job_intelligence"
    )
    op.drop_table("job_intelligence")


def _touch_updated_at() -> None:
    """Bind the shared trigger so `updated_at` means what every table's does."""
    op.execute("DROP TRIGGER IF EXISTS job_intelligence_touch_updated_at ON job_intelligence")
    op.execute(
        """
        CREATE TRIGGER job_intelligence_touch_updated_at
        BEFORE UPDATE ON job_intelligence
        FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
        """
    )


def _enforce_rls() -> None:
    """FORCE, not merely ENABLE — see `20260727_1900_extraction_tables.py`."""
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
