"""opportunity document attempts: bound the extraction loop

Revision ID: c1v2m0000001
Revises: c1v2l0000001
Create Date: 2026-08-14 12:00:00+00:00

`opportunity_documents` was the one LLM-paying table without an attempt
bound. Every other model-paying row (`candidate_imports`, `sourcing_runs`,
`job_intelligence`, `candidate_intelligence`, `candidate_documents`)
counts `attempts` at pickup and parks itself in `failed` past a ceiling, so
a document that crashes the pipeline every time is re-enqueued by
`rescan_stuck` a bounded number of times and then stops. `opportunity_documents`
had no such column: a job-description file whose extraction timed out was
re-enqueued by the sweep forever — one `extract_opportunity_document` job
per sweep, each billing up to several model calls (2-pass extraction x 3
transport retries + no-content retry), until a human noticed.

The `stalled_opportunity_documents` resolver is unchanged — the sweep still
returns stalled rows; the *job* is what refuses past the ceiling and parks
the row in `failed`, so the sweep stops seeing it.

Revision ID: c1v2m0000001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2m0000001"
down_revision: str | None = "c1v2l0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "opportunity_documents",
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("opportunity_documents", "attempts")
