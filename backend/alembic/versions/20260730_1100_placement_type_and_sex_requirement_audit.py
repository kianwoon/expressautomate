"""audit who set placement_type and the occupational sex requirement

`placement_type` unlocks a lawful sex filter for an MDW placement
(`app/api/candidates.py::list_candidates`, `eligible_for`). Mislabelling an
ordinary job as one is now the way to get that filter with legal cover, and
until now nothing recorded who set it. `updated_at`/`updated_by` on the row
are too blunt — they answer "who last touched this opportunity", not "who
made this specific, consequential call" — a later edit to `job_title_raw`
would silently overwrite the same trail.

Same reasoning for `sex_requirement`/`sex_requirement_reason`: a genuine
occupational requirement is a human judgement the law leaves to a person, and
the record must say who made it.

Both pairs are nullable: existing rows, and any row where the field is
cleared back to NULL, simply have no "set by" to report.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "6b1e9f4d7a20"
down_revision: str | None = "2fb4171f2a02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "opportunities",
        sa.Column(
            "placement_type_set_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "opportunities", sa.Column("placement_type_set_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "opportunities",
        sa.Column(
            "sex_requirement_set_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "opportunities", sa.Column("sex_requirement_set_at", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("opportunities", "sex_requirement_set_at")
    op.drop_column("opportunities", "sex_requirement_set_by")
    op.drop_column("opportunities", "placement_type_set_at")
    op.drop_column("opportunities", "placement_type_set_by")
