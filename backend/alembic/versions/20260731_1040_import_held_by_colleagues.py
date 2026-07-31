"""candidate_imports records the rows it would not touch

An import that silently applies fewer rows than the file contained looks like
a bug to the recruiter who ran it. The count sits beside the other tallies so
the imports table can say "and 3 belong to colleagues" rather than nothing.

Revision ID: c1a0d5e7b205
Revises: c1a0d5e7b204
"""

import sqlalchemy as sa

from alembic import op

revision: str = "c1a0d5e7b205"
down_revision: str | None = "c1a0d5e7b204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `server_default` as well as NOT NULL: existing rows predate the column
    # and there is no per-row answer to backfill, and zero is the truth for
    # them — no import before this one ever skipped anything for ownership.
    op.add_column(
        "candidate_imports",
        sa.Column(
            "held_by_colleagues",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("candidate_imports", "held_by_colleagues")
