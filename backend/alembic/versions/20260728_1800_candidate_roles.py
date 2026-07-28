"""candidate roles

The RLS policy is created in the same revision as the table on purpose.
`verify_rls_enforced()` (`app/db/rls.py:58`) refuses to boot on any readable
table without a forced policy, so a policy added in a later revision is a
failed deploy rather than a silent cross-tenant leak.
"""

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision = "b7c1e4a2d905"
down_revision = "1519048c9751"
branch_labels = None
depends_on = None

_SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "candidate_roles",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employer", sa.Text(), nullable=False),
        sa.Column("employer_normalized", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_normalized", sa.Text(), nullable=False),
        sa.Column("started_on", sa.Date()),
        sa.Column("started_precision", sa.String(8)),
        sa.Column("ended_on", sa.Date()),
        sa.Column("ended_precision", sa.String(8)),
        sa.Column("employment_type", sa.String(32)),
        sa.Column("location", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("source", sa.String(24), nullable=False, server_default="human"),
        sa.Column("status", sa.String(16), nullable=False, server_default="confirmed"),
        sa.Column("extraction_id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("updated_by", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_roles_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on",
            name="ck_candidate_roles_ends_after_start",
        ),
    )
    op.create_index("ix_candidate_roles_tenant_id", "candidate_roles", ["tenant_id"])
    op.create_index("ix_candidate_roles_candidate_id", "candidate_roles", ["candidate_id"])
    op.create_index("ix_candidate_roles_status", "candidate_roles", ["status"])
    op.create_index("ix_candidate_roles_employer_normalized", "candidate_roles", ["employer_normalized"])
    op.create_index("ix_candidate_roles_title_normalized", "candidate_roles", ["title_normalized"])
    op.create_index(
        "ix_candidate_roles_candidate_started", "candidate_roles", ["candidate_id", "started_on"]
    )

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON candidate_roles TO "{settings.DATABASE_APP_ROLE}"')

    predicate = f"tenant_id = nullif(current_setting('{_SETTING}', true), '')::uuid"
    op.execute("ALTER TABLE candidate_roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE candidate_roles FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_roles")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON candidate_roles
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_roles")
    op.drop_table("candidate_roles")
