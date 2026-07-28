"""Extraction provenance (plan §14, §15).

`Extraction` is keyed on the email, not the opportunity: one model run may find
three vacancies or none, and the run that found none is exactly the one worth
inspecting later. Replay appends a row; nothing is updated in place, so an
email's extraction history is the ordered set of its rows.

The evidence table is what turns "the model said SGD 6,000" into something
checkable. Each field carries the span it came from, and application code
verifies that span against the source before the value is trusted — which is
how §15's "must not fabricate" becomes a property of the system rather than an
instruction the model is asked to honour.
"""

import uuid

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Extraction(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "extractions"

    # Nullable since a CV extraction has no email behind it. The CHECK below is
    # what keeps this honest: provenance that names no source, or two, is not
    # provenance at all.
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("email_messages.id", ondelete="CASCADE"),
        index=True,
    )
    # `candidate_documents` now exists (Task 2), so the ForeignKey can be
    # declared here alongside the migration that creates the table and adds
    # the DB-level constraint.
    candidate_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidate_documents.id", ondelete="CASCADE"),
        index=True,
    )
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "(email_message_id IS NULL) <> (candidate_document_id IS NULL)",
            name="ck_extractions_exactly_one_source",
        ),
    )


class ExtractionEvidence(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "extraction_evidence"

    extraction_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("extractions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), index=True
    )
    # Both stay nullable with no CHECK between them: evidence may legitimately
    # name neither when it describes a field of the source document itself
    # (e.g. "this CV has no listed graduation year"), so do not "fix" this
    # into an exactly-one-source constraint the way Extraction has one.
    candidate_role_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidate_roles.id", ondelete="CASCADE"), index=True
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    extracted_value: Mapped[str | None] = mapped_column(Text)
    evidence_text: Mapped[str | None] = mapped_column(Text)
    start_char: Mapped[int | None] = mapped_column(Integer)
    end_char: Mapped[int | None] = mapped_column(Integer)
    # Retained for calibration work, never shown to a user as a probability.
    model_confidence: Mapped[float | None] = mapped_column(Float)
    evidence_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class OpportunityFieldOverride(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A human correction. Replay must never overwrite one of these."""

    __tablename__ = "opportunity_field_overrides"

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    ai_value: Mapped[str | None] = mapped_column(Text)
    human_value: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
