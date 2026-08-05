"""Shorthand found in the email that produced a job order (plan §14, §15).

Every column here exists so an interpretation can be re-checked years later.
`start_char`/`end_char` point at the literal code in the preprocessed email —
so "the client asked for a Chinese female" can always be reduced to "the email
says `C/F` at 412–415", which a recruiter can see for themselves.

**`meaning` and `attribute` are snapshots, not a foreign key.** This is the
whole design decision. A `glossary_code_id` would be smaller and would always
show the current definition — which is precisely wrong: an agency that edits
what `C/F` means in March would silently rewrite every job order read in
January, and an audit trail that changes retroactively is not an audit trail.
The row records the interpretation that was *applied at the time*. The cost is
that a corrected glossary does not fix old rows, and that is the right cost:
fixing them is a decision someone should take deliberately, on the record.

For the same reason there is no FK to `glossary_codes`. Deleting a code must
not cascade away the evidence that it was once used.
"""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class OpportunityCode(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One occurrence of one glossary code, decoded for one job order."""

    __tablename__ = "opportunity_codes"

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The literal text as it appeared, sliced from the source by the scanner —
    # `C / F` stays `C / F` rather than being tidied into the glossary's
    # spelling, because this column has to match what a reviewer will read.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False)
    attribute: Mapped[str | None] = mapped_column(String(32))
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        # One row per detected occurrence: a re-extraction that re-runs the
        # deterministic `detect()` finds the same spans, and without this guard
        # it would write them again. Keyed on offset, not on code alone, because
        # a code genuinely appearing twice in one email lands at two offsets and
        # both are real, distinct occurrences.
        UniqueConstraint(
            "opportunity_id",
            "code",
            "start_char",
            "end_char",
            name="uq_opportunity_codes_once_per_span",
        ),
    )
