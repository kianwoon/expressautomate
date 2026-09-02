"""External candidate searches: our record of one career-bot task.

The career bot (spec: `docs/` of the career bot project, CANDIDATES-API.md)
runs the search and keeps the results; the browser polls them through this
API. One row per started search, and the row exists for one reason only:
**the career bot's task ids are polled through our shared API key**, so the
task id must never be accepted on its own word. Without a tenant-scoped row,
any signed-in recruiter who guessed (or overheard) another agency's task id
could read that agency's candidate results through us — plan §18. The row is
the membership check: task id AND tenant AND opportunity must all agree, and
RLS scopes the tenant on top.

Results ARE persisted here now, on the row itself. The passthrough design
assumed the career bot retains results; 2026-09-02 proved otherwise — a task
polled at 14:28 was a 404 on their side by 14:41 the same day, and a panel
that had shown ten ranked candidates showed nothing an hour later. When a
read returns a completed task, its ranked list (the career bot's spec §4
shape, verbatim) is written onto this row, so opening the job order again
reloads the search from us instead of discovering the task has expired. The
source-provenance rule in CLAUDE.md applies to that copy from this day on:
each result carries its evidence (`source`, `source_url`, `match_reason`,
`credibility`), and retention follows the row — deleting the job order
deletes the searches and their results (CASCADE).
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class ExternalCandidateSearch(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "external_candidate_searches"

    # Plain FK, not composite — see `CandidateSubmission.opportunity_id` in
    # `app/models/sourcing.py` for why: a composite `SET NULL` would null
    # `tenant_id` too. Deleting the job order deletes the searches run for it
    # (CASCADE), because a search's results are only ever reached through its
    # job order's modal.
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The career bot's task id, verbatim — a UUID string on their side, but
    # stored as text so their format can change without a migration.
    task_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # Who clicked Find External Candidates. A share recipient may run a
    # search on work shown to them — visibility, not edit rights, the same
    # reading `run_intelligence`'s exemption takes.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # The career bot's ranked list (its spec §4 shape), written when a read
    # finds the task completed. NULL while the task is still working — and
    # after a failure or a pause, which get no results to show, so a NULL
    # here plus a stale task on the career bot's side simply means "run a
    # fresh search".
    results: Mapped[list | None] = mapped_column(JSONB)

    # When the task reached its terminal state, from the same completed read
    # that wrote `results`. The freshness line the panel shows is dated from
    # this, not from created_at — a search the recruiter left running across
    # lunch completed when it completed, not when they clicked.
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
