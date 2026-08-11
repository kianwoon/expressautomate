"""One row per relevance-gate verdict — the gate's cost provenance.

The gate is the highest-volume LLM call in the system, and it is the only one
with no recorded spend: `extractions` keeps prompt/completion tokens per
extraction, but nothing answered "what did the gate cost per email". This
table mirrors the `extractions` cost columns so a per-email report can join
the two with one vocabulary.
"""

import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class ClassificationUsage(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "classification_usages"

    email_message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("email_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Mirrors `extractions`: the model that actually answered (OpenRouter may
    # route elsewhere than asked), the prompt version under which it answered,
    # and the token counts that turn a report into a bill.
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
