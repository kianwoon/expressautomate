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

Results are deliberately not persisted: the career bot retains them and every
read is a passthrough, so no copy of external candidates' profile data lands
in our database and carries no retention obligation (the source-provenance
rule in CLAUDE.md). If results are ever stored locally, that rule applies to
them from that day on.
"""

import uuid

from sqlalchemy import ForeignKey, Text
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
