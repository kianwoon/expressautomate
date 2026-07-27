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

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Opportunity(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "opportunities"

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

    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ready", index=True
    )
    quality_state: Mapped[str] = mapped_column(String(16), nullable=False, default="likely")
