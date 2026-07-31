"""candidate_field_overrides gains user_id; existing rows stay tenant-wide

Revision ID: c1a0d5e7b204
Revises: c1a0d5e7b203
"""

import sqlalchemy as sa

from alembic import op

revision: str = "c1a0d5e7b204"
down_revision: str | None = "c1a0d5e7b203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidate_field_overrides", sa.Column("user_id", sa.UUID(), nullable=True)
    )
    op.create_index(
        "ix_candidate_field_overrides_user_id", "candidate_field_overrides", ["user_id"]
    )
    op.create_foreign_key(
        "fk_candidate_overrides_user_same_tenant",
        "candidate_field_overrides",
        "users",
        ["tenant_id", "user_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    # Existing rows keep user_id NULL on purpose. They were written to protect
    # a field from a later import for the whole agency, not to express one
    # recruiter's view, and backfilling them to `changed_by` would quietly
    # convert agency-wide protection into private opinion.
    op.drop_constraint(
        "uq_candidate_overrides_one_per_field",
        "candidate_field_overrides",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_candidate_overrides_one_per_field_per_user",
        "candidate_field_overrides",
        ["tenant_id", "candidate_id", "user_id", "field_name"],
    )
    op.create_index(
        "uq_candidate_overrides_one_tenant_wide_per_field",
        "candidate_field_overrides",
        ["tenant_id", "candidate_id", "field_name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )


def downgrade() -> None:
    # NOT SAFE once any per-user row exists. `uq_candidate_overrides_one_per_field`
    # is keyed on (tenant_id, candidate_id, field_name) alone, so two rows for
    # the same field with different `user_id` values — exactly what the PATCH
    # upsert's JUDGEMENT_FIELDS branch writes — collide on it and this
    # `create_unique_constraint` raises `UniqueViolation`. This migration adds
    # no backfill or delete to make room for it, on purpose: silently
    # collapsing two recruiters' readings into one row on downgrade would lose
    # data with no log of what was lost. An operator downgrading past this
    # revision must first decide, per colliding field, which user's row wins
    # (or export/delete the losers) rather than have that decided for them.
    op.drop_index(
        "uq_candidate_overrides_one_tenant_wide_per_field",
        table_name="candidate_field_overrides",
    )
    op.drop_constraint(
        "uq_candidate_overrides_one_per_field_per_user",
        "candidate_field_overrides",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_candidate_overrides_one_per_field",
        "candidate_field_overrides",
        ["tenant_id", "candidate_id", "field_name"],
    )
    op.drop_constraint(
        "fk_candidate_overrides_user_same_tenant",
        "candidate_field_overrides",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_candidate_field_overrides_user_id", table_name="candidate_field_overrides"
    )
    op.drop_column("candidate_field_overrides", "user_id")
