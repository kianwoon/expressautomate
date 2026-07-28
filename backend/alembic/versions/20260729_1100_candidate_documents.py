"""candidate documents

Creates `candidate_documents` — a CV a candidate came with — with RLS enabled
and forced in the same revision, for the reason `20260728_1800_candidate_
roles.py` gives: `verify_rls_enforced()` refuses to boot on a readable table
without a forced policy, so a policy added later would be a silent
cross-tenant leak in the gap.

Also lands the two things `20260729_1000_extraction_sources.py` deliberately
deferred until this table existed: the FK on
`extractions.candidate_document_id`, and the "exactly one source" CHECK the
ORM model already declares. And adds `candidate_skills.source`/`.status`,
mirroring `candidate_roles`, so a parsed skill can arrive as an unconfirmed
proposal instead of a person typing it directly.

Revision ID: d1e6a7b04f33
Revises: c4d9f1a83e12
Create Date: 2026-07-29 11:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "d1e6a7b04f33"
down_revision: str | None = "c4d9f1a83e12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "candidate_documents",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("text_key", sa.Text()),
        sa.Column("text_chars", sa.Integer()),
        sa.Column("parse_state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("parse_error", sa.Text()),
        sa.Column("uploaded_by", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_documents_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "parse_state IN ('pending','parsing','parsed','unreadable','failed')",
            name="ck_candidate_documents_parse_state",
        ),
    )
    op.create_index("ix_candidate_documents_tenant_id", "candidate_documents", ["tenant_id"])
    op.create_index("ix_candidate_documents_candidate_id", "candidate_documents", ["candidate_id"])

    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON candidate_documents '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )

    predicate = f"tenant_id = nullif(current_setting('{_SETTING}', true), '')::uuid"
    op.execute("ALTER TABLE candidate_documents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE candidate_documents FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_documents")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON candidate_documents
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )

    # candidate_skills: source/status, mirroring candidate_roles. Server
    # defaults make every existing row read as human-typed and confirmed,
    # so no data migration is needed.
    op.add_column(
        "candidate_skills",
        sa.Column("source", sa.String(24), nullable=False, server_default="human"),
    )
    op.add_column(
        "candidate_skills",
        sa.Column("status", sa.String(16), nullable=False, server_default="confirmed"),
    )

    # Deferred from Task 1's migration: now that candidate_documents exists,
    # the FK it anchors and the "exactly one source" CHECK can be created.
    op.create_foreign_key(
        "fk_extractions_candidate_document",
        "extractions",
        "candidate_documents",
        ["candidate_document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_extractions_exactly_one_source",
        "extractions",
        "(email_message_id IS NULL) <> (candidate_document_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_extractions_exactly_one_source", "extractions", type_="check")
    op.drop_constraint(
        "fk_extractions_candidate_document", "extractions", type_="foreignkey"
    )

    op.drop_column("candidate_skills", "status")
    op.drop_column("candidate_skills", "source")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_documents")
    op.drop_table("candidate_documents")
