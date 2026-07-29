"""candidate imports

Creates `candidate_imports` and `candidate_import_changes` — a spreadsheet a
recruiter uploaded to bulk-load or bulk-update candidates, and the per-field
trail that lets it be undone. RLS is enabled and forced in the same
revision, for the reason `20260729_1100_candidate_documents.py` gives:
`verify_rls_enforced()` refuses to boot on a readable table without a forced
policy, so a policy added later would be a silent cross-tenant leak in the
gap.

`candidate_import_changes` carries both `previous_value` and `new_value`
rather than `previous_value` alone. The undo this table exists for (Task 6)
restores a field only if its current value still equals what the import
wrote — a recruiter who retyped it afterwards owns it now. Evaluating that
rule needs both sides of the comparison; with only `previous_value` there is
nothing to check the current value against, and undo cannot tell "the import
still owns this" from "a human already overwrote it".

`candidates.import_id` and `candidate_roles.import_id` land here too, each a
plain (non-composite) FK with `ON DELETE SET NULL`: an import is a record of
an event, and deleting that record must never delete the person or role it
created. A composite `(tenant_id, import_id)` FK — the idiom every other
table in this file uses — was considered and rejected: Postgres's bare
`ON DELETE SET NULL` on a composite key nulls every referencing column
including `tenant_id`, which is NOT NULL on both tables, so the delete would
fail instead of detaching the reference. `candidates.merged_into_candidate_id`
hit the same trap for the same reason.

Revision ID: f3a8c92d1e07
Revises: e2f7b8c15a44
Create Date: 2026-07-29 13:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "f3a8c92d1e07"
down_revision: str | None = "e2f7b8c15a44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "candidate_imports",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("error_report_key", sa.Text()),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("candidates_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("roles_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("roles_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_candidate_imports_tenant_id_id"),
        sa.CheckConstraint(
            "state IN ('pending','parsing','done','failed','undone')",
            name="ck_candidate_imports_state",
        ),
    )
    op.create_index("ix_candidate_imports_tenant_id", "candidate_imports", ["tenant_id"])

    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON candidate_imports '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )

    predicate = f"tenant_id = nullif(current_setting('{_SETTING}', true), '')::uuid"
    op.execute("ALTER TABLE candidate_imports ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE candidate_imports FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_imports")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON candidate_imports
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )

    op.create_table(
        "candidate_import_changes",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("previous_value", sa.Text()),
        sa.Column("new_value", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "import_id"],
            ["candidate_imports.tenant_id", "candidate_imports.id"],
            name="fk_candidate_import_changes_import_same_tenant",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "entity_type IN ('candidate','role')",
            name="ck_candidate_import_changes_entity_type",
        ),
        sa.CheckConstraint(
            "action IN ('created','updated')",
            name="ck_candidate_import_changes_action",
        ),
    )
    op.create_index(
        "ix_candidate_import_changes_tenant_id", "candidate_import_changes", ["tenant_id"]
    )
    op.create_index(
        "ix_candidate_import_changes_import_id", "candidate_import_changes", ["import_id"]
    )
    op.create_index(
        "ix_candidate_import_changes_entity_id", "candidate_import_changes", ["entity_id"]
    )

    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON candidate_import_changes '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )

    op.execute("ALTER TABLE candidate_import_changes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE candidate_import_changes FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_import_changes")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON candidate_import_changes
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )

    # candidates.import_id / candidate_roles.import_id: plain FKs (see module
    # docstring for why not composite), SET NULL so deleting an import record
    # never deletes the person or role it created.
    op.add_column(
        "candidates", sa.Column("import_id", sa.dialects.postgresql.UUID(as_uuid=True))
    )
    op.create_foreign_key(
        "fk_candidates_import",
        "candidates",
        "candidate_imports",
        ["import_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "candidate_roles", sa.Column("import_id", sa.dialects.postgresql.UUID(as_uuid=True))
    )
    op.create_foreign_key(
        "fk_candidate_roles_import",
        "candidate_roles",
        "candidate_imports",
        ["import_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_candidate_roles_import", "candidate_roles", type_="foreignkey")
    op.drop_column("candidate_roles", "import_id")
    op.drop_constraint("fk_candidates_import", "candidates", type_="foreignkey")
    op.drop_column("candidates", "import_id")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_import_changes")
    op.drop_table("candidate_import_changes")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_imports")
    op.drop_table("candidate_imports")
