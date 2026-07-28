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
    CheckConstraint,
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

    # Mirrors `CandidateRole.source`/`.status`: every existing row is
    # human-typed, so the server defaults make the backfill correct with no
    # data migration. Once the CV parser lands (Task 4) a skill can arrive as
    # a proposal instead — `source="cv_upload"`, `status="unconfirmed"` —
    # and wait for a person to confirm or reject it, same lifecycle a role has.
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="human")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")

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


class CandidateRole(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One job a candidate held.

    The level beneath the flat candidate row: `current_title` says where
    somebody is, this says how they got there, which is the part sourcing can
    reason about.

    Dates carry their own precision because a CV that says "Mar 2019" does not
    say the day. Storing `2019-03-01` and rendering "1 March 2019" would assert
    a fact no source ever stated (§15), so the precision travels with the date
    and the UI renders only what was actually known.

    `source` and `status` have exactly one value each today — a recruiter types
    these rows and they are confirmed on arrival. They exist now because the CV
    parser and the importers land in this same table later, and adding the
    columns then means a migration across every tenant's live data.
    """

    __tablename__ = "candidate_roles"

    HUMAN = "human"
    SOURCES = (HUMAN, "cv_upload", "email_attachment", "import")

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    STATUSES = (UNCONFIRMED, CONFIRMED, REJECTED)

    PRECISIONS = ("year", "month", "day")

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )

    # Raw beside normalised, the same rule `opportunities` and `candidates`
    # follow: the recruiter recognises what they typed, and only the normalised
    # form can be compared against a job order's company name.
    employer: Mapped[str] = mapped_column(Text, nullable=False)
    employer_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # Nullable because a CV that gives no dates at all is still worth recording
    # — the employer and title alone are a matchable fact. A NULL `ended_on`
    # with a non-NULL `started_on` means the role is current.
    started_on: Mapped[date | None] = mapped_column(Date)
    started_precision: Mapped[str | None] = mapped_column(String(8))
    ended_on: Mapped[date | None] = mapped_column(Date)
    ended_precision: Mapped[str | None] = mapped_column(String(8))

    employment_type: Mapped[str | None] = mapped_column(String(32))
    location: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    source: Mapped[str] = mapped_column(String(24), nullable=False, default=HUMAN)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CONFIRMED, index=True
    )
    # Set only on a row a model produced, so the evidence behind it can be
    # found. Always NULL while a person is the only writer.
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_roles_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        # No unique constraint on (candidate, employer, title, started_on).
        # Somebody can genuinely hold the same title at the same employer
        # twice, having left and returned, and refusing the second one would
        # be the system telling a recruiter their own record is wrong.
        CheckConstraint(
            "ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on",
            name="ck_candidate_roles_ends_after_start",
        ),
        Index("ix_candidate_roles_candidate_started", "candidate_id", "started_on"),
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


class CandidateDocument(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A CV a candidate came with.

    The extracted text is stored (`text_key`, `text_chars`) rather than
    re-derived on demand, because an evidence span (`ExtractionEvidence.
    start_char`/`end_char`) is an offset into it — the exact bytes the
    extraction saw have to still exist for that offset to mean anything
    later. The original file (`object_key`) is kept separately for that same
    reason: re-extracting text from a re-parsed PDF is not guaranteed to
    reproduce the same offsets a stored extraction already points into.

    `unreadable` and `failed` are two different terminal states, not one,
    because they answer different questions. A corrupt or scanned-image PDF
    is `unreadable` — asking again gets the same answer forever, so the UI
    should stop offering retry. A timeout or a transient parser crash is
    `failed` — a bad minute, worth trying again without anyone editing
    anything.
    """

    __tablename__ = "candidate_documents"

    PENDING = "pending"
    PARSING = "parsing"
    PARSED = "parsed"
    # Text came out of the file, the model read it, and it described no role
    # and named no skill. Terminal like `parsed`, and separate from it because
    # a blank candidate panel otherwise looks the same as a full one that has
    # not loaded — see the `cv_parse_outcome` migration.
    EMPTY = "empty"
    UNREADABLE = "unreadable"
    FAILED = "failed"
    PARSE_STATES = (PENDING, PARSING, PARSED, EMPTY, UNREADABLE, FAILED)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )

    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # The R2 object key for the uploaded file as received.
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    # The R2 object key for the extracted plain text, set once parsing
    # succeeds. Nullable because most of a document's life is spent before
    # that — `pending`, `parsing`, or a terminal failure that never produced
    # text.
    text_key: Mapped[str | None] = mapped_column(Text)
    text_chars: Mapped[int | None] = mapped_column(Integer)

    parse_state: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDING)
    parse_error: Mapped[str | None] = mapped_column(Text)

    # What the extractor read but would not publish. A role or skill whose
    # quotation is not on the page is discarded (`cv.extract`), and without a
    # count beside the document a recruiter has no way to tell "the CV listed
    # three jobs" from "two of the five were thrown away". `dropped_reason` is
    # the sentence shown next to it; both stay set on a `parsed` document, so
    # this is a note about a success, not an error.
    dropped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dropped_reason: Mapped[str | None] = mapped_column(Text)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_documents_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "parse_state IN ('pending','parsing','parsed','empty','unreadable','failed')",
            name="ck_candidate_documents_parse_state",
        ),
    )
