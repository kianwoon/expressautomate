"""extraction sources

An extraction can now describe a CV, not only an email: `email_message_id`
becomes nullable and `candidate_document_id` joins it. The FK on
`candidate_document_id` and the "exactly one source" CHECK are deliberately
NOT added here — `candidate_documents` does not exist until the next
migration, which adds both. Until then the ORM model's `__table_args__`
declares a constraint the database does not yet enforce; that is expected.

`extraction_evidence.candidate_role_id` is added alongside `opportunity_id`
for the same reason, but stays uncoupled to any CHECK — evidence may
legitimately name neither column.

Revision ID: c4d9f1a83e12
Revises: b7c1e4a2d905
Create Date: 2026-07-29 10:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d9f1a83e12"
# Re-parented onto the opportunity-vocabulary revision rather than the
# candidate-roles one this was written against. Both were authored off
# `b7c1e4a2d905` on separate branches, which left two heads at the merge, and
# the other branch reached production first. Following it keeps the history a
# single line, which is the only shape `alembic upgrade head` can act on.
down_revision: str | None = "3f9a6c21e8b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("extractions", "email_message_id", existing_type=sa.UUID(), nullable=True)
    op.add_column("extractions", sa.Column("candidate_document_id", sa.UUID(), nullable=True))
    op.create_index(
        op.f("ix_extractions_candidate_document_id"),
        "extractions",
        ["candidate_document_id"],
        unique=False,
    )

    op.add_column(
        "extraction_evidence", sa.Column("candidate_role_id", sa.UUID(), nullable=True)
    )
    op.create_index(
        op.f("ix_extraction_evidence_candidate_role_id"),
        "extraction_evidence",
        ["candidate_role_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_extraction_evidence_candidate_role_id",
        "extraction_evidence",
        "candidate_roles",
        ["candidate_role_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_extraction_evidence_candidate_role_id", "extraction_evidence", type_="foreignkey"
    )
    op.drop_index(
        op.f("ix_extraction_evidence_candidate_role_id"), table_name="extraction_evidence"
    )
    op.drop_column("extraction_evidence", "candidate_role_id")

    op.drop_index(op.f("ix_extractions_candidate_document_id"), table_name="extractions")
    op.drop_column("extractions", "candidate_document_id")
    op.alter_column("extractions", "email_message_id", existing_type=sa.UUID(), nullable=False)
