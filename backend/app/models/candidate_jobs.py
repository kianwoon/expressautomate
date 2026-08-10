"""The last "Find Job" shortlist for one candidate.

`CandidateJobShortlist` is one row per candidate holding the snapshot of the
last Find Job run: the serialized job orders (their scores and the breakdowns
behind them) plus the run's bookkeeping. The scores are computed synchronously
by `app/api/candidate_jobs.py`; this row is what lets the Jobs tab in the
candidate modal reopen and show the last result without re-running — a read of
one row, not a re-score of every visible vacancy in the agency.

The items are JSONB, the same call `SourcingMatch.reasons` makes: the snapshot
is read as a unit beside the candidate and never queried on individually, and
storing the serialization (rather than opportunity ids to re-resolve) keeps a
later vacancy edit from silently changing what the recruiter was shown.
"""

import uuid

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class CandidateJobShortlist(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """The shortlist shown on a candidate's Jobs tab, as last run."""

    __tablename__ = "candidate_job_shortlists"

    # No individual FK on `candidate_id`: it forms the composite
    # `(tenant_id, candidate_id)` FK to `candidates` below, the same shape
    # `SourcingMatch` takes. `created_by` is a plain FK to `users`, which has
    # no tenant pair to form.
    candidate_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # How many visible current revisions were examined, how many produced a
    # score, and how many were kept. Mirrors `SourcingRun`'s
    # candidates_considered / shortlisted pair so the modal can say "best N of
    # M scoreable (of K visible)".
    considered: Mapped[int] = mapped_column(Integer, nullable=False)
    scored: Mapped[int] = mapped_column(Integer, nullable=False)
    limit: Mapped[int] = mapped_column(Integer, nullable=False)

    # The candidate's salary expectation as of the run — null when incomplete,
    # so the salary absence can be phrased as a candidate-level fact rather
    # than the scorer's generic "one side is missing" wording.
    candidate_salary: Mapped[dict | None] = mapped_column(JSONB)

    # The serialized shortlist, exactly as the modal renders it — see the
    # module docstring for why the snapshot is stored rather than re-derived.
    items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_job_shortlists_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        # One snapshot per candidate: re-running Find Job replaces the row
        # rather than appending a history, exactly as `candidate_intelligence`
        # upserts on the same pair.
        UniqueConstraint(
            "tenant_id", "candidate_id", name="uq_candidate_job_shortlists_once_per_candidate"
        ),
    )
