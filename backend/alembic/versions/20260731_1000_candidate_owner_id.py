"""candidates.owner_id, backfilled from created_by

Revision ID: c1a0d5e7b201
Revises: 314cc3da9ced
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c1a0d5e7b201"
down_revision: str | None = "314cc3da9ced"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidates", sa.Column("owner_id", sa.UUID(), nullable=True))
    op.create_index("ix_candidates_owner_id", "candidates", ["owner_id"])
    # Alembic emits the column list correctly — precedent in
    # 20260731_0900_client_assignee_column_qualified_set_null.py, which exists
    # because `clients.assigned_user_id` shipped with the bare form.
    op.create_foreign_key(
        "fk_candidates_owner_same_tenant",
        "candidates",
        "users",
        ["tenant_id", "owner_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL (owner_id)",
    )
    # `created_by` is the closest honest answer available: every existing
    # candidate was typed by somebody. Imported and seeded rows have no
    # `created_by`, stay NULL, and land in the claimable queue.
    op.execute("UPDATE candidates SET owner_id = created_by WHERE created_by IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint("fk_candidates_owner_same_tenant", "candidates", type_="foreignkey")
    op.drop_index("ix_candidates_owner_id", table_name="candidates")
    op.drop_column("candidates", "owner_id")
