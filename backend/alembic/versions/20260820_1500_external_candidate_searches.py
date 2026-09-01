"""external_candidate_searches: our tenant-scoped record of one career-bot task

Revision ID: c1v2q0000001
Revises: c1v2p0000001
Create Date: 2026-08-20 15:00:00+00:00

The career bot runs candidate searches and keeps the results; the browser
polls them through this API. The row exists for one reason: the career bot's
task ids are polled with OUR API key, so a task id alone must never be
accepted on its own word. Without a tenant-scoped row, any signed-in
recruiter who guessed another agency's task id could read that agency's
candidate results through us (plan §18). The row is the membership check —
task id AND tenant AND opportunity must all agree — and RLS scopes the
tenant on top.

Created from the start with FORCE RLS, the way `opportunity_documents` was:
`verify_rls_enforced()` refuses to boot if a `tenant_id` table has no policy,
so this table must carry a `tenant_isolation` policy or the deployment will
not start.

Results are deliberately NOT stored here — every read is a passthrough to
the career bot, so no copy of external candidates' profile data lands in our
database and carries no retention obligation (the source-provenance rule in
CLAUDE.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2q0000001"
down_revision: str | None = "c1v2p0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED: list[tuple[str, str]] = [("external_candidate_searches", "tenant_id")]
SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "external_candidate_searches",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        # Composite FK to `opportunities(tenant_id, id)` — a search row can
        # never name another agency's job order — CASCADE so deleting the
        # job order removes the searches run for it.
        sa.ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.id"],
            ondelete="CASCADE",
            name="fk_external_candidate_searches_opportunity_same_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_external_candidate_searches_tenant_id"),
        "external_candidate_searches",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_candidate_searches_opportunity_id"),
        "external_candidate_searches",
        ["opportunity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_external_candidate_searches_task_id"),
        "external_candidate_searches",
        ["task_id"],
        unique=False,
    )

    _enforce_rls()
    _touch_updated_at()


def downgrade() -> None:
    op.drop_index(
        op.f("ix_external_candidate_searches_task_id"),
        table_name="external_candidate_searches",
    )
    op.drop_index(
        op.f("ix_external_candidate_searches_opportunity_id"),
        table_name="external_candidate_searches",
    )
    op.drop_index(
        op.f("ix_external_candidate_searches_tenant_id"),
        table_name="external_candidate_searches",
    )
    op.drop_table("external_candidate_searches")


def _touch_updated_at() -> None:
    """Bind the shared trigger so `updated_at` means what every table's does."""
    op.execute(
        "DROP TRIGGER IF EXISTS external_candidate_searches_touch_updated_at "
        "ON external_candidate_searches"
    )
    op.execute(
        """
        CREATE TRIGGER external_candidate_searches_touch_updated_at
        BEFORE UPDATE ON external_candidate_searches
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
