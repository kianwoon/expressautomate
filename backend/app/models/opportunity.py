"""One vacancy (plan §16, §17, §25).

Analytics-ready from the first row. Retrofitting `salary_period` onto a year of
data means re-reading a year of emails; carrying a nullable column costs
nothing. `job_family` and `seniority` exist for the same reason and stay empty
until there is a controlled vocabulary — free-form model categories do not
aggregate, which is the opposite of the point.

Every extracted value keeps its `_raw` form beside any normalised one. The raw
string is what a recruiter recognises and what evidence offsets point at; the
normalised one is what a query groups by. Storing only the second would make a
disagreement with the source impossible to see.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Opportunity(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "opportunities"

    LOCAL_HIRE = "local_hire"
    MDW_WORK_PERMIT = "mdw_work_permit"
    OTHER_WORK_PERMIT = "other_work_permit"
    S_PASS = "s_pass"
    EMPLOYMENT_PASS = "employment_pass"
    PLACEMENT_TYPES = (LOCAL_HIRE, MDW_WORK_PERMIT, OTHER_WORK_PERMIT, S_PASS, EMPLOYMENT_PASS)

    email_message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("email_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised from the email so listing and filtering never needs the join.
    received_datetime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    company_name_raw: Mapped[str | None] = mapped_column(Text)
    company_name_normalized: Mapped[str | None] = mapped_column(Text, index=True)
    job_title_raw: Mapped[str | None] = mapped_column(Text)
    job_title_normalized: Mapped[str | None] = mapped_column(Text, index=True)
    job_family: Mapped[str | None] = mapped_column(String(64))
    seniority: Mapped[str | None] = mapped_column(String(32))

    job_description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    industry: Mapped[str | None] = mapped_column(String(128))

    employment_type: Mapped[str | None] = mapped_column(String(32))
    work_arrangement: Mapped[str | None] = mapped_column(String(32))
    working_hours_raw: Mapped[str | None] = mapped_column(Text)

    salary_min: Mapped[float | None] = mapped_column(Numeric(12, 2))
    salary_max: Mapped[float | None] = mapped_column(Numeric(12, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    # "SGD 6,000" is not a number until you know per what. Without the period a
    # monthly and an annual figure average together into nonsense.
    salary_period: Mapped[str | None] = mapped_column(String(16))
    salary_raw: Mapped[str | None] = mapped_column(Text)

    duration_raw: Mapped[str | None] = mapped_column(Text)
    duration_months: Mapped[int | None] = mapped_column(Integer)

    location_raw: Mapped[str | None] = mapped_column(Text)
    location_normalized: Mapped[str | None] = mapped_column(Text, index=True)

    # What kind of placement this vacancy is — local hire, a migrant domestic
    # worker's Work Permit, another Work Permit, S Pass or Employment Pass.
    # NULL means not stated: a recruiter has not classified the vacancy yet,
    # which is different from every classification being wrong.
    #
    # Set only by a person, via the API, never inferred. In particular this is
    # never derived from the `EP`/`SP`/`WP` glossary codes a client's email may
    # contain: `app/api/glossary.py` marks those `attribute="nationality"`,
    # which makes them protected, and `redact.py` strips them from every piece
    # of text a model reads before extraction runs. Deriving a regulatory fact
    # from the very text that redaction exists to scrub would re-import
    # through the back door exactly what that module removes at the front —
    # `tests/test_opportunity_placement_type.py` asserts an email containing
    # `WP` leaves this column NULL.
    placement_type: Mapped[str | None] = mapped_column(String(32))

    # Who set `placement_type`, and when — `updated_at`/`created_at` (from
    # `Timestamps`) answer "when did *something* on this row last change",
    # which is too blunt once `placement_type` unlocks a lawful sex filter
    # (`app/api/candidates.py::list_candidates`, `eligible_for`). Mislabelling
    # an ordinary job as an MDW placement is now the way to get that filter
    # with legal cover, so the record needs to say who made the call, not just
    # that the row moved. SET NULL, the same idiom as `Candidate.created_by`/
    # `updated_by`: the user who set it may later be deleted, and the fact
    # that *someone* set it must outlive the account that did.
    placement_type_set_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    placement_type_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # A sex requirement the job itself imposes (a genuine occupational
    # requirement — e.g. intimate personal care for an elderly client) and the
    # recruiter's own words for why. Distinct from `Candidate.sex`, which is a
    # fact about a person, and from the coded client preference `C/F` decodes
    # to, which is recorded as evidence and never acted on (see
    # `app/api/glossary.py`). One cannot exist without the other — an
    # unexplained sex requirement is exactly what Singapore law does not
    # permit — enforced by `ck_opportunities_sex_requirement_has_reason` below
    # and mirrored at the API in `app/api/opportunities.py`.
    sex_requirement: Mapped[str | None] = mapped_column(String(16))
    sex_requirement_reason: Mapped[str | None] = mapped_column(Text)
    # Same audit idiom as `placement_type_set_by`/`_set_at`, and for the same
    # reason: an occupational requirement is a human judgement call, not a
    # law, and the record must say who made it.
    sex_requirement_set_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    sex_requirement_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ready", index=True
    )
    quality_state: Mapped[str] = mapped_column(String(16), nullable=False, default="likely")

    __table_args__ = (
        # Both vocabularies were guaranteed only by the function that wrote
        # them, and a guarantee that lives in one caller is one direct INSERT
        # away from being untrue. That is not hypothetical here: `salary_period`
        # went in raw from the extraction for months, so "Month" and " month "
        # reached the column, and the salary sort silently dropped those rows
        # to the bottom of the list because it looked the period up by the
        # canonical word.
        #
        # Stated here so the database refuses what the readers cannot handle.
        # `_salary_period` in ingest/persist.py is what keeps writes inside
        # this set — it maps what a model answers onto one of these words and
        # yields NULL for anything it cannot read, so an odd phrasing is a
        # missing period rather than a failed ingestion.
        CheckConstraint(
            "salary_period IS NULL OR salary_period IN "
            "('hour', 'day', 'week', 'month', 'year')",
            name="ck_opportunities_salary_period_known",
        ),
        # NOT NULL already; this pins the values. `quality_state()` in
        # ingest/evidence.py is the sole writer and returns exactly these
        # three, so this records a property the code already has rather than
        # imposing a new one.
        CheckConstraint(
            "quality_state IN ('needs_review', 'likely', 'verified')",
            name="ck_opportunities_quality_state_known",
        ),
        CheckConstraint(
            "placement_type IS NULL OR placement_type IN "
            "('local_hire', 'mdw_work_permit', 'other_work_permit', "
            "'s_pass', 'employment_pass')",
            name="ck_opportunities_placement_type_known",
        ),
        CheckConstraint(
            "sex_requirement IS NULL OR sex_requirement IN ('female', 'male')",
            name="ck_opportunities_sex_requirement_known",
        ),
        # The pairing rule itself. `IS NULL OR` guards `sex_requirement`
        # rather than the pair, because the constraint must refuse a
        # requirement with no reason while still allowing a reason-less,
        # requirement-less row — the ordinary case for most vacancies.
        CheckConstraint(
            "(sex_requirement IS NULL) = (sex_requirement_reason IS NULL)",
            name="ck_opportunities_sex_requirement_has_reason",
        ),
    )
