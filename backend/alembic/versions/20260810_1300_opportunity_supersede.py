"""opportunity supersede revision link

Revision ID: c1v2d0000001
Revises: c1v2c0000001
Create Date: 2026-08-10 13:00:00+00:00

A later email that *changes* an already-held open job order's requirements
(sex, race, salary, hours...) is a revision, not a duplicate. The old row is
kept — append-only extraction history is the audit trail — and pointed at the
new row that replaced it. `superseded_by_opportunity_id` carries that link;
`superseded_at` when it was made.

The dedupe and the list read this column to hide superseded rows in favour of
their successor, while matching consumers (`load_visible_opportunity`) follow
the link so eligibility, sourcing and job intelligence always read the current
revision's requirements — never the stale ones the client replaced.

The FK is composite and tenant-scoped, matching every other cross-agency
boundary in the schema, and uses column-qualified SET NULL (PG15+) so deleting
the successor clears only `superseded_by_opportunity_id`, never `tenant_id`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c1v2d0000001"
down_revision: str | None = "c1v2c0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "opportunities",
        sa.Column(
            "superseded_by_opportunity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "opportunities",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_opportunities_superseded_same_tenant",
        "opportunities",
        "opportunities",
        ["tenant_id", "superseded_by_opportunity_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL (superseded_by_opportunity_id)",
    )
    op.create_index(
        "ix_opportunities_superseded_by_opportunity_id",
        "opportunities",
        ["superseded_by_opportunity_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_opportunities_superseded_by_opportunity_id", table_name="opportunities"
    )
    op.drop_constraint(
        "fk_opportunities_superseded_same_tenant", "opportunities", type_="foreignkey"
    )
    op.drop_column("opportunities", "superseded_at")
    op.drop_column("opportunities", "superseded_by_opportunity_id")
