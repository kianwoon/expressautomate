"""The stored result of a Job Intelligence analysis — one row per job order.

Phase 1 stores the latest analysis only, keyed uniquely on the opportunity, so
re-running the analysis overwrites the previous result rather than accumulating
a history. That is the simplest shape that serves the "Job Intelligence" button
in the job order modal: a recruiter asks for the analysis and reads it back, and
a re-ask replaces what they saw before. A history of analyses is a Phase 2
concern (compare runs after a JD edit) and would warrant its own table; the
unique constraint here is what lets that future table be added without a
breaking change to this one.

The three JSONB columns mirror the three Pydantic models in
`app.services.job_intelligence.schema`, stored verbatim from the model's answer.
They are JSONB rather than normalised columns for the same reason
`SourcingMatch.reasons` is: they are read as a unit and never queried on
individually, so joined tables would buy nothing and cost a join on every read.

`removed_codes` records which protected-attribute glossary codes `redact()`
stripped before the model saw the job order — the same audit property
`SourcingRun.protected_attribute_note` records for sourcing. It is a list rather
than free text so a future view can render each code distinctly.

`state` is a state machine like `SourcingRun.state`, for the same reason: the
analysis runs as an arq job (the three LLM calls have no business inside an HTTP
request, and every other LLM call in the system runs in the worker process where
the LLM provider is configured — not the api process). A row sits at `pending` from the
POST until the worker claims it, moves to `running`, then `done` or `failed`.
`rescan_stuck` re-enqueues rows stranded at `pending` or `running`, mirroring
sourcing. `attempts` is spent at the claim so a job order that crashes the
pipeline every time reaches `failed` rather than re-running forever.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class JobIntelligence(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """The latest Job Intelligence analysis for one job order."""

    __tablename__ = "job_intelligence"

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STATES = (PENDING, RUNNING, DONE, FAILED)

    # Plain FK — `opportunities` has no `(tenant_id, id)` unique constraint for a
    # composite FK to reference, the same constraint that shaped
    # `SourcingRun.opportunity_id` and `CandidateSubmission.opportunity_id`.
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The state machine. `pending` from POST until the worker claims it; `done`
    # with results, or `failed` with a sentence. Mirrors `SourcingRun.state`.
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PENDING, index=True
    )
    # Why a `failed` run failed, in a sentence a recruiter can act on. Same
    # role as `SourcingRun.failure_reason`.
    failure_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    understanding: Mapped[dict | None] = mapped_column(JSONB)
    persona: Mapped[dict | None] = mapped_column(JSONB)
    search_plan: Mapped[dict | None] = mapped_column(JSONB)

    # The occupation benchmark match (Module 4): the MOM occupation the
    # analysis matched the work to, with its P25/Median/P75 and the LLM's
    # confidence/rationale. JSONB for the same reason as the three above: read
    # as a unit, never queried on individually. NULL when the match stage
    # degraded (no embeddings configured, empty library) rather than an empty
    # object — distinguishes "ran and found nothing" from "never ran".
    occupation: Mapped[dict | None] = mapped_column(JSONB)

    # The model name recorded on the *last* of the three calls. The three calls
    # normally share one model, so one name suffices; recording three would be
    # detail the button never shows.
    model_name: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    # The protected-attribute codes stripped before the model saw the job order.
    # NULL (rather than an empty list) distinguishes "redaction ran and removed
    # nothing" from "this row predates the audit column" — the same reasoning
    # that makes every nullable-by-default audit column nullable.
    removed_codes: Mapped[list | None] = mapped_column(JSONB)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    analysed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # One analysis per job order. Re-running the button upserts; the unique
        # constraint is what makes that an UPDATE rather than a second row.
        UniqueConstraint(
            "tenant_id", "opportunity_id", name="uq_job_intelligence_one_per_opportunity"
        ),
        CheckConstraint(
            "state IN ('pending','running','done','failed')",
            name="ck_job_intelligence_state",
        ),
    )

