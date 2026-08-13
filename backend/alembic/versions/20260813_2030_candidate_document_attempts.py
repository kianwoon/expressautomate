"""candidate document attempts: bound the CV parse loop

Revision ID: c1v2l0000001
Revises: c1v2k0000001
Create Date: 2026-08-13 20:30:00+00:00

A CV parse was the one LLM-paying job with no attempt bound. Every other
model-paying row (`candidate_imports`, `sourcing_runs`,
`job_intelligence`, `candidate_intelligence`) counts `attempts` at pickup
and parks itself in `failed` past a ceiling, so a document that crashes the
pipeline every time is re-enqueued by `rescan_stuck` a bounded number of
times and then stops. `candidate_documents` had no such column: a CV whose
parse timed out was re-enqueued by the sweep forever — one `parse_candidate_cv`
job per sweep, each up to several billed model calls, until a human noticed
(2026-08-13: `TimeoutError` storm on arq, jobs `delayed=600-712s`, the
DeepSeek bill climbing on CVs that never parsed).

This adds `attempts` and a ceiling. The column is counted at claim time in
`parse_candidate_cv` and `ingest_candidate_cv` exactly as `import_jobs`
counts its own: a worker killed mid-call never reaches an end, so a count
spent at completion would count nothing on precisely the runs this bounds.
`stalled_candidate_documents` is unchanged — the sweep still returns stalled
rows; the *job* is what refuses past the ceiling and parks the row in
`failed`, so the sweep stops seeing it.

Revision ID: c1v2l0000001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2l0000001"
down_revision: str | None = "c1v2k0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candidate_documents",
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("candidate_documents", "attempts")
