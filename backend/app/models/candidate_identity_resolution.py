"""Candidate identity resolutions: the outcome of one Identity Resolver run.

Spec: `serper design.md` §24 (the resolved record), §34/§35 (the cache), §59
(the state machine), §47/§48 (provenance, retention).

One row per resolution run. External candidates live inside an opportunity's
`external_candidate_searches.results` JSONB, not in their own table, so a
candidate has no stable row id to key on — the resolver keys on
`candidate_key` (the panel's own per-result id) *within* the job order, and on
`fingerprint_hash` for the §34 cache.

The row exists for two reasons:

1. **The cache** (§34). Reopening a candidate must not re-spend Serper quota.
   The newest unexpired row for `(opportunity, candidate_key, fingerprint_hash)`
   is the cached result; a `refresh`/`deep` run deliberately bypasses it.
2. **The evidence trail** (§47). `evidence` is the identity evidence verbatim,
   each item carrying the source URL it came from, so a recruiter can see *why*
   a candidate was resolved and a future calibration can read the outcome
   (§46). Retention follows the row: deleting the job order deletes its
   resolutions (CASCADE), the same lifecycle as the searches they annotate.

Source provenance (CLAUDE.md) applies: these rows hold third-party public-web
data with its sources attached, and are not permanent candidate records.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

# §16 — the three resolution states. Stored as a bounded string rather than a
# Postgres enum: adding `stale` (§59) later must not need a type migration.
STATUS_RESOLVED = "resolved"
STATUS_PROBABLE = "probable"
STATUS_UNRESOLVED = "unresolved"


class CandidateIdentityResolution(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "candidate_identity_resolutions"

    # Plain FK, not composite — the same reason `ExternalCandidateSearch.
    # opportunity_id` uses one: a composite `SET NULL` would null `tenant_id`
    # too. CASCADE so the evidence lifecycle matches the job order's.
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The panel's own id for one external-candidate result (`ExternalCandidate.
    # id`), which is what the Resolve button was clicked on. Text, not UUID:
    # it is the career bot's id, and its format is theirs to change.
    candidate_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # §6 — the fingerprint the run was built from, kept whole so a result can
    # be understood without the (mutable) external search row it came from.
    fingerprint: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # §34 — hash(normalized_name + company + title + location), the cache key.
    # A changed fingerprint (a recruiter edited the candidate) is a cache miss
    # by construction, which is the "invalidate early" rule in §35.
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # §16/§24 — `resolved` / `probable` / `unresolved`.
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    # §24 — identityConfidence, 0–100.
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)

    # §24 — the professional profile URL `contact enrichment` prefers (§26).
    canonical_profile_url: Mapped[str | None] = mapped_column(Text)

    # §24 — the canonical identity, preferring the web's view where it exists,
    # falling back to the fingerprint's record.
    current_company: Mapped[str | None] = mapped_column(Text)
    current_title: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    previous_companies: Mapped[list | None] = mapped_column(JSONB)

    # §15/§24/§47 — the weighted evidence, each item carrying its source URL.
    evidence: Mapped[list | None] = mapped_column(JSONB)

    # §24/§43 — how many Serper queries this run spent (the cost line).
    queries_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # §21 — `current` / `possible_change` / `unknown`.
    freshness_status: Mapped[str | None] = mapped_column(String(24))

    # §34/§35 — when this cached result stops being reused. A run past this is
    # re-resolved on the next click even without an explicit `refresh`.
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Who clicked Resolve Identity. Visibility, not edit rights, the same
    # reading the external-search routes take.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
