"""A job-description file attached to a job order.

The row exists to carry bytes that arrive from a browser into R2, then to hand
the extraction's answer back to the create-dialog form for review. It mirrors
`CandidateDocument`'s shape deliberately: same hostile-until-proven rules
(computed key, sniffed kind, capped size), same parse-state vocabulary
(`pending` → `extracting` → `extracted` / `unreadable` / `failed`).

Unlike a CV, the original file is kept indefinitely — it is the vacancy's
source of truth, the document the recruiter may want to re-read or forward, and
the audit trail for the extracted values. `prefill` stores what the extraction
model said, in the form's own vocabulary; the same anti-fabrication discipline
as email extraction applies, so a value the document never mentions is `null`,
never a guess.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class OpportunityDocument(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "opportunity_documents"

    # Nullable because the create-dialog flow stores the file before the
    # vacancy exists; `create_opportunity` writes the link when the form
    # carries a `document_id`. CASCADE on the composite FK removes the row
    # with its opportunity.
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), index=True
    )

    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    # The R2 object key for the uploaded file as received. Computed from the
    # authenticated tenant and a freshly minted document id — never from the
    # filename the browser sent.
    object_key: Mapped[str] = mapped_column(Text, nullable=False)

    PENDING = "pending"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    UNREADABLE = "unreadable"
    FAILED = "failed"
    EXTRACT_STATES = (PENDING, EXTRACTING, EXTRACTED, UNREADABLE, FAILED)

    extract_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PENDING
    )
    extract_error: Mapped[str | None] = mapped_column(Text)

    # How many times a worker has picked this document up. Counted at pickup
    # rather than at completion, for the reason `CandidateDocument.attempts`
    # gives: the run this bounds is the one that never completes — a document
    # whose extraction times out or crashes leaves the row non-terminal,
    # `rescan_stuck` re-enqueues it, and without a count that survives the
    # crash the pair loops forever, one `extract_opportunity_document` job per
    # sweep, each billing up to several model calls. Past
    # `OPPORTUNITY_DOCUMENT_MAX_ATTEMPTS` the job parks the row in `failed`
    # instead, so the sweep stops seeing it.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # The extracted values handed to the create-dialog form for review, keyed
    # by the form's own field names (`job_title_raw`, `salary_raw`, …). Null
    # until the worker maps the extraction; a field the document never mentions
    # is absent from the JSON rather than a fabricated value.
    prefill: Mapped[dict | None] = mapped_column(JSONB)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.id"],
            name="fk_opportunity_documents_opportunity_same_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "extract_state IN ('pending','extracting','extracted','unreadable','failed')",
            name="ck_opportunity_documents_extract_state",
        ),
    )
