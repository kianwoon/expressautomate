"""email llm_attempts: bound the classify/extract/replay rebill loop

Revision ID: c1v2n0000001
Revises: c1v2m0000001
Create Date: 2026-08-14 14:00:00+00:00

`email_messages` was the one LLM-paying table without an attempt bound.
Every other model-paying row (`candidate_imports`, `sourcing_runs`,
`job_intelligence`, `candidate_intelligence`, `candidate_documents`,
`opportunity_documents`) counts `attempts` at pickup and parks itself in
`failed` past a ceiling, so a row that crashes the pipeline every time is
re-enqueued by `rescan_stuck` a bounded number of times and then stops.

Email rows had `attempt_count`, but it is fetch-scoped: `_RECORD_FETCH`
increments it once per healthy life, so a ceiling on it would fail every
email that reaches extraction. This column counts something different and
exactly right: **sweep-recovery pickups of the LLM-paying jobs** — a row
resumed while ALREADY in its working status (`classifying`, `extracting`,
`replaying`) is a crash-loop iteration by definition, because forward
progress never re-enters a status it already holds. The first classify
(fetched→classifying) and the first extract (classified→extracting) do not
increment; every recovery pickup does, and every replay pickup does (a
replay is always deliberate spend, and the resolver has already moved the
row to `replaying` before the job first sees it, so first-vs-recovery is
not distinguishable there — three lifetime replays per email is the bound).

The job — not the sweep — refuses past `EMAIL_LLM_MAX_ATTEMPTS` and parks
the row `failed`, exactly as the CV parse bound (c1v2l0000001) does:
`stalled_email_rows` is unchanged and still returns the row; the job is
what stops paying.

Revision ID: c1v2n0000001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1v2n0000001"
down_revision: str | None = "c1v2m0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "email_messages",
        sa.Column(
            "llm_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("email_messages", "llm_attempts")
