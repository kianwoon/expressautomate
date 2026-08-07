"""The stored result of a Candidate Intelligence analysis — one row per candidate.

Mirrors `JobIntelligence` in shape: Phase 1 stores the latest analysis only,
keyed uniquely on the candidate, so re-running the analysis overwrites the
previous result rather than accumulating a history. The "Candidate analysis"
button in the candidate modal asks for the analysis and reads it back, and a
re-ask replaces what the recruiter saw before.

The three JSONB columns (`career` / `capability` / `profile`) mirror the three
Pydantic models in `app.services.candidate_intelligence.schema`, stored verbatim
from the model's answer. They are JSONB rather than normalised columns for the
same reason `JobIntelligence`'s three are: they are read as a unit and never
queried on individually, so joined tables would buy nothing and cost a join on
every read.

The composite FK `(tenant_id, candidate_id) → candidates(tenant_id, id)` keeps
the tenant boundary explicit — the same pattern `CandidateRole` uses, and
possible here because `candidates` carries the `uq_candidates_tenant_id_id`
unique constraint that `opportunities` lacks (which is why `JobIntelligence`
settled for a plain FK).

`state` is a state machine like `JobIntelligence.state`, for the same reason:
the analysis runs as an arq job (the three LLM calls have no business inside an
HTTP request, and every other LLM call in the system runs in the worker process
where Cerebras is configured — not the api process). A row sits at `pending`
from the POST until the worker claims it, moves to `running`, then `done` or
`failed`. `rescan_stuck` re-enqueues rows stranded at `pending` or `running`.
`attempts` is spent at the claim so a candidate that crashes the pipeline every
time reaches `failed` rather than re-running forever.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class CandidateIntelligence(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """The latest Candidate Intelligence analysis for one candidate."""

    __tablename__ = "candidate_intelligence"

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STATES = (PENDING, RUNNING, DONE, FAILED)

    # Composite FK to `candidates(tenant_id, id)`, possible because `candidates`
    # carries the `uq_candidates_tenant_id_id` unique constraint. CASCADE so
    # deleting a candidate removes their analysis with them.
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )

    # The state machine. `pending` from POST until the worker claims it; `done`
    # with results, or `failed` with a sentence. Mirrors `JobIntelligence.state`.
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PENDING, index=True
    )
    # Why a `failed` run failed, in a sentence a recruiter can act on.
    failure_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    career: Mapped[dict | None] = mapped_column(JSONB)
    capability: Mapped[dict | None] = mapped_column(JSONB)
    profile: Mapped[dict | None] = mapped_column(JSONB)

    # The model name recorded on the *last* of the three calls. The three calls
    # normally share one model, so one name suffices.
    model_name: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    analysed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # One analysis per candidate. Re-running the button upserts; the unique
        # constraint is what makes that an UPDATE rather than a second row.
        UniqueConstraint(
            "tenant_id", "candidate_id", name="uq_candidate_intelligence_one_per_candidate"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_intelligence_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ('pending','running','done','failed')",
            name="ck_candidate_intelligence_state",
        ),
    )
