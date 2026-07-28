"""One person an agency places.

Unlike a client, a candidate is never proposed by the pipeline. Email carries
job orders, not CVs — the classifier is binary (`ingest/classify.py:28`) and
drops anything that is not a job order before extraction runs, and attachments
are never downloaded. So every value here has a human author, which is a
stronger provenance claim than any extraction makes and is why none of the
evidence or confidence machinery appears.

Identity is email or phone, either alone. The common case is a recruiter's
older sheet carrying a personal address and the newer one a work address, with
the mobile unchanged; requiring both to agree would duplicate the person every
time. Name is never a key — two different people share a name far more often
than intuition suggests, and merging two real people is worse than a duplicate
somebody can merge later.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Candidate(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "candidates"

    ACTIVE = "active"
    ARCHIVED = "archived"
    MERGED = "merged"
    STAGES = ("new", "contacted", "submitted", "placed", "rejected")

    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    # Stored twice on purpose. `phone_raw` is what the recruiter typed and what
    # they recognise; `phone_e164` is the only form two rows can be compared
    # on. Same raw-beside-normalised rule `opportunities` follows.
    phone_raw: Mapped[str | None] = mapped_column(String(64))
    phone_e164: Mapped[str | None] = mapped_column(String(32))

    current_title: Mapped[str | None] = mapped_column(Text)
    current_employer: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    years_experience: Mapped[int | None] = mapped_column(Integer)
    expected_salary: Mapped[float | None] = mapped_column(Numeric(12, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    # Without the period a monthly and an annual figure average into nonsense
    # — see the comment on `opportunities.salary_period`.
    salary_period: Mapped[str | None] = mapped_column(String(16))
    available_from: Mapped[date | None] = mapped_column(Date)
    notice_period_raw: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(String(32))

    notes: Mapped[str | None] = mapped_column(Text)

    # The R2 object key for the candidate's photo, shown only inside the
    # candidate modal. Nullable because most candidates never get one — a
    # human uploads it deliberately, unlike everything else on this row.
    avatar_key: Mapped[str | None] = mapped_column(Text)
    avatar_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Where the person is in the process, and whether the row is still real.
    # Separate columns because they answer different questions: collapsing them
    # means archiving somebody destroys the fact that they were placed.
    pipeline_stage: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", index=True
    )
    record_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ACTIVE, index=True
    )
    merged_into_candidate_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_candidates_tenant_id_id"),
        # CASCADE, and it is the erasure rule rather than a convenience.
        #
        # A row merged into this one is a duplicate record of the same human
        # being, so erasing the person must erase it too — leaving it would
        # keep their name and email in the table a deletion request just
        # cleared.
        #
        # SET NULL cannot work here whichever form it takes. Bare `SET NULL`
        # on a composite key nulls every referencing column including
        # `tenant_id`, which is NOT NULL; the Postgres 15+
        # `SET NULL (merged_into_candidate_id)` form clears only the pointer
        # but then violates `ck_candidates_merged_has_target`, which requires
        # a merged row to name one. Both fail the delete.
        ForeignKeyConstraint(
            ["tenant_id", "merged_into_candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidates_merged_into_same_tenant",
            ondelete="CASCADE",
        ),
        # Declared here as well as in the migration so autogenerate does not
        # propose dropping them. `merged` is excluded so a merge frees both
        # keys for the surviving row; `archived` stays inside, because an
        # archived person still holds their identity and an import that
        # skipped them would collide on insert instead.
        Index(
            "uq_candidates_tenant_email",
            "tenant_id",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("email IS NOT NULL AND record_status <> 'merged'"),
        ),
        Index(
            "uq_candidates_tenant_phone",
            "tenant_id",
            "phone_e164",
            unique=True,
            postgresql_where=text("phone_e164 IS NOT NULL AND record_status <> 'merged'"),
        ),
    )


class CandidateSkill(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A skill is a row, not an array element, because it is searched on."""

    __tablename__ = "candidate_skills"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    skill: Mapped[str] = mapped_column(Text, nullable=False)
    skill_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_skills_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "skill_normalized",
            name="uq_candidate_skills_once_per_candidate",
        ),
    )


class CandidateFieldOverride(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A field a person edited, which no import may overwrite.

    The shape is borrowed from `opportunity_field_overrides`, but that table is
    a model and nothing else — nothing reads or writes it. There is no working
    implementation to copy, so this is new machinery. The justification differs
    too: there it guards against an AI re-extraction clobbering a human, here
    the only thing that would overwrite is a later import of a stale sheet.
    """

    __tablename__ = "candidate_field_overrides"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    human_value: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_overrides_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "field_name",
            name="uq_candidate_overrides_one_per_field",
        ),
    )
