"""external_candidate_searches: keep the results the career bot forgets

Revision ID: c1v2t0000001
Revises: c1v2s0000001
Create Date: 2026-09-02 16:00:00+00:00

The career bot's task ids expire — 2026-09-02 proved it: a search started at
14:28 was a 404 on their side by 14:41 the same afternoon, and the panel that
had shown ten ranked candidates hours earlier showed nothing. The passthrough
design assumed a retention the service does not have, so the results are now
written onto the search row itself when a completed read happens, and the
panel reloads them from us the next time the job order is opened.

`results` is the career bot's ranked list verbatim (spec §4) — JSONB for the
same reason `job_intelligence.search_plan` is: read as a unit, never queried
on individually. `finished_at` marks the terminal read: a search still in
flight has no results yet, and a search that failed or was paused has none it
should be showing, so both stay NULL and the panel treats "no saved results"
as "run a fresh search" either way.

The source-provenance rule (CLAUDE.md) applies from this day on: these rows
now hold third-party profile data with evidence (`source`, `source_url`,
`match_reason`, `credibility`) carried inside each result, and retention
follows the row — deleting the job order deletes the searches (CASCADE).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "c1v2t0000001"
down_revision: str | None = "c1v2s0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "external_candidate_searches",
        sa.Column("results", JSONB(), nullable=True),
    )
    op.add_column(
        "external_candidate_searches",
        sa.Column(
            "finished_at", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("external_candidate_searches", "finished_at")
    op.drop_column("external_candidate_searches", "results")
