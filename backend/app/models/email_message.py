"""Raw email metadata (plan §10, §19; §2.3 as amended).

Bodies live in R2; this row holds everything queryable plus the keys to them.

Three independent state machines are kept in three columns on purpose:

- `processing_status`     — how far our pipeline got
- `source_state`          — what the mailbox looks like now
- `classification_status` — what the relevance gate decided

Conflating the first two is a real bug, not a tidiness question: Graph's message
delta is folder-scoped, so an `@removed` event usually means the recruiter filed
the mail into a subfolder, not that it was deleted. One column would invalidate
the opportunities extracted from it on a folder move.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

# pending → fetched → classifying → classified → extracting → a terminal below.
#
# `classified` is not decoration. Leaving a verdicted row at `classifying`
# makes "the gate is running" and "the gate has answered, extraction has not
# started" the same state, and `rescan_stuck` resolves that ambiguity the
# expensive way: it re-enqueues `classify_email`, which pays the gate again for
# a row that already has an answer, every RESCAN_WORKING_MINUTES, forever.
PROCESSING_TERMINAL = frozenset(
    {"extracted", "no_opportunity", "skipped", "unfetchable", "failed"}
)


class EmailMessage(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "email_messages"
    __table_args__ = (
        # Both identifiers, because Graph's `id` changes when a message moves
        # folders — hence the ImmutableId preference on every request.
        #
        # Scoped to mailbox_id deliberately: two recruiters at one agency CC'd
        # on the same job email share an internet_message_id, and a tenant-wide
        # constraint would silently drop the second recruiter's copy. Deciding
        # those rows describe one vacancy is opportunity-level dedup (§19),
        # which is a separate problem.
        UniqueConstraint(
            "tenant_id", "mailbox_id", "graph_message_id", name="uq_email_mailbox_graph_id"
        ),
        UniqueConstraint(
            "tenant_id",
            "mailbox_id",
            "internet_message_id",
            name="uq_email_mailbox_internet_id",
        ),
        # `rescan_stuck` runs every few minutes and asks exactly one question:
        # which rows are stalled in a non-terminal status? A partial index
        # covers only those rows, so the sweep never pays for the extracted
        # ones — which, in a working system, are almost all of them.
        Index(
            "ix_email_messages_stalled",
            "processing_status",
            "updated_at",
            postgresql_where=text(
                "processing_status IN ('pending', 'fetched', 'classifying',"
                " 'classified', 'extracting')"
            ),
        ),
    )

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("mailboxes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    graph_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    internet_message_id: Mapped[str | None] = mapped_column(Text)
    conversation_id: Mapped[str | None] = mapped_column(Text)

    sender_name: Mapped[str | None] = mapped_column(String(256))
    sender_email: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(Text)
    received_datetime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    has_attachments: Mapped[bool | None] = mapped_column(Boolean)

    body_r2_key: Mapped[str | None] = mapped_column(Text)
    body_html_r2_key: Mapped[str | None] = mapped_column(Text)

    # server_default, not just default: the whole pipeline inserts and updates
    # these rows with raw SQL (the webhook must be fast and the jobs run without
    # the ORM), and a Python-side default never fires there — it fails the
    # NOT NULL constraint at runtime instead.
    processing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True
    )
    source_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="present", server_default="present"
    )
    classification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    classification_reason: Mapped[str | None] = mapped_column(Text)
    classification_model: Mapped[str | None] = mapped_column(String(128))
    classification_version: Mapped[str | None] = mapped_column(String(32))

    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
