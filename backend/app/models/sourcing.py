"""Candidate sourcing: ranking candidates against a job order (plan, sourcing epic).

Three tables, none of them candidate-owned, which is why they live apart from
the 524-line `app/models/candidate.py` rather than growing it further:

`SourcingRun` is one attempt to rank a job order's candidates. `SourcingMatch`
is one candidate's result within that run — score, reasons, and (once Task 5
lands) an explanation with the evidence behind it. `CandidateSubmission` is
the one durable fact a run's output is meant to produce: that a person was
actually put in front of a client. It carries no status workflow and no
stages on purpose — "in front of this client or not" is a boolean, not a
pipeline, and modelling it as one would let the same person be proposed to
the same client twice while looking like two different, equally valid facts.

`SourcingRun.protected_attribute_noticed`/`_note` exist because the model
that produces a run (Task 5) is instructed to report a plainly discriminatory
requirement it encounters in the job order — e.g. an age ceiling or a marital
status filter. Without a column for that report to land in, the instruction
is a comment nobody reads rather than a safeguard a recruiter can act on.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class CandidateSubmission(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A candidate was put in front of a client. Nothing more, nothing less.

    Unique on `(tenant_id, candidate_id, client_id)` — a person is either in
    front of that client or not, so a double-click on "submit" (or a rerun of
    the sourcing job that produced the same recommendation) must not create a
    second row. `opportunity_id` is kept only as the *reason* a submission
    happened, and is nullable because a recruiter can submit a candidate to a
    client directly, outside any tracked job order.
    """

    __tablename__ = "candidate_submissions"

    candidate_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # Plain FK, not composite — see the `CandidateImport.import_id` comment in
    # candidate.py for why: a composite `SET NULL` would null `tenant_id`
    # too, which is NOT NULL here. `opportunities` also has no
    # `(tenant_id, id)` unique constraint for a composite FK to reference.
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="SET NULL")
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_submissions_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_candidate_submissions_client_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "client_id",
            name="uq_candidate_submissions_once_per_client",
        ),
    )


class SourcingRun(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One attempt to rank a job order's candidates.

    `attempts` mirrors `CandidateImport.attempts` — counted at pickup so a
    worker crash mid-run is visible and `RUNNING` rows do not loop forever
    once a stuck-run sweep exists.

    `protected_attribute_noticed`/`_note`: the model that produces a run is
    instructed (Task 5) to flag a plainly discriminatory requirement it reads
    in the job order rather than silently ranking around it. These columns
    are where that flag lands — a report with nowhere to go is a comment, not
    a safeguard.
    """

    __tablename__ = "sourcing_runs"

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STATES = (PENDING, RUNNING, DONE, FAILED)

    # Plain FK — see `CandidateSubmission.opportunity_id` for why: no
    # `(tenant_id, id)` unique constraint exists on `opportunities` yet.
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    state: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDING)
    candidates_considered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shortlisted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    model_name: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    protected_attribute_noticed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    protected_attribute_note: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_sourcing_runs_tenant_id_id"),
        CheckConstraint(
            "state IN ('pending','running','done','failed')",
            name="ck_sourcing_runs_state",
        ),
    )


class SourcingMatch(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One candidate's result within a `SourcingRun`.

    `explanation`/`explanation_evidence` are nullable because Task 5's model
    pass produces `score`/`reasons` for every candidate it ranks, but the
    prose explanation (and the evidence behind it) is only generated for the
    ones that make the shortlist — generating it for every candidate
    considered would be the expensive part of the run multiplied by every
    row that never surfaces to a recruiter.
    """

    __tablename__ = "sourcing_matches"

    run_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    # A list of short, evidence-backed reasons the model gave for the score —
    # JSONB rather than a joined table because these are read as a unit
    # alongside the score and never queried on individually.
    reasons: Mapped[dict | list] = mapped_column(JSONB, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text)
    explanation_evidence: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            name="fk_sourcing_matches_run_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_sourcing_matches_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "run_id", "candidate_id", name="uq_sourcing_matches_once_per_run"
        ),
        Index("ix_sourcing_matches_run_id", "run_id"),
    )
