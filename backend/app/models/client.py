"""One company an agency recruits for.

A client is proposed by the pipeline and owned by a human. The distinction is
the whole design: `status` starts at `unconfirmed` and only a recruiter moves
it, because the evidence for "these two emails are the same company" is a
domain match at best and a normalised string at worst, and neither is a fact.

Identity is the sender's email domain. It is the only stable key the pipeline
actually has — a company renames itself in prose far more often than it
changes its mail domain. The normalised name exists to *propose* a match to a
person, never to make one.

Provenance lives in `client_mentions`, not here. This row is what a recruiter
edits; the mentions are the record of what the mail said, and one must not be
able to overwrite the other.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Client(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "clients"

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    MERGED = "merged"
    ARCHIVED = "archived"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    # A hint for proposing a match to a person, never a key. Two unrelated
    # firms normalise to the same string often enough that a unique index here
    # would reject legitimate rows.
    name_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # NULL when the sender was on a free provider — see settings.FREE_EMAIL_DOMAINS.
    email_domain: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=UNCONFIRMED, index=True
    )
    merged_into_client_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    # SET NULL, not CASCADE: a client must outlive the email that produced it.
    first_seen_email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        # Children reference (tenant_id, id) so their FK cannot cross agencies.
        UniqueConstraint("tenant_id", "id", name="uq_clients_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "merged_into_client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_clients_merged_into_same_tenant",
            ondelete="SET NULL",
        ),
    )


class ClientMention(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One email that referred to one client. The evidence trail.

    `ON DELETE SET NULL` on the message, so a retention purge of the mail body
    cannot erase the record that the client was ever seen. A mention with a
    null message id says "this happened and the source is gone", which is
    true; a deleted mention would say "this never happened", which is not.
    """

    __tablename__ = "client_mentions"

    client_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="SET NULL"), index=True
    )
    matched_by: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_client_mentions_client_same_tenant",
            ondelete="CASCADE",
        ),
        # One mention per client per message. `extract_email` re-runs after a
        # crash and replay appends; without this every rerun duplicates the
        # evidence and the mention count stops meaning anything.
        UniqueConstraint(
            "tenant_id",
            "client_id",
            "email_message_id",
            name="uq_client_mentions_once_per_message",
        ),
    )
