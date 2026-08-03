"""Who gets told what, where, and whether it arrived (spec 2026-07-28).

Four tenant-scoped tables and one deliberately global one.

`notification_deliveries` is the load-bearing table and does three jobs at
once. It is the outbox — Redis cannot join the Postgres transaction that
committed the opportunity, the same gap `workers/queue.py` documents and fails
soft on, so a notification with no row would simply be lost. It is the dedupe
key, since `(destination_id, event_kind, subject_id)` answers "have we already
said this" without state anywhere else. And it is the rate-cap counter, so the
cap is a query over the rows themselves and cannot drift from them.
"""

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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

CHANNEL_TELEGRAM = "telegram"
CHANNEL_WHATSAPP = "whatsapp"
# WhatsApp over the recruiter's *own* paired device (`wa_sessions`), not the
# shared WABA. A separate channel rather than a flag on the one above: the two
# share no client, no message shape and no reputation, and `whatsapp_linked`
# can only ever be one person's number, so a tenant-wide destination on it is
# impossible by construction.
CHANNEL_WHATSAPP_LINKED = "whatsapp_linked"

STATUS_PENDING = "pending"
# Claimed by a worker. The gap between claim and send is why this exists: two
# workers racing on one row must produce one message, not two.
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SUPPRESSED = "suppressed"


def address_digest(address: str) -> str:
    """A stable, non-reversible handle for an address.

    An encrypted column cannot carry a unique index — Fernet output differs on
    every call for the same input — so uniqueness and lookup run on this
    instead. SHA-256 with no salt on purpose: a per-row salt would make equal
    addresses hash differently, which is exactly what must not happen.
    """
    return hashlib.sha256(address.encode()).hexdigest()


class NotificationDestination(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """Where messages go."""

    __tablename__ = "notification_destinations"

    # Null means the destination belongs to the tenant rather than a person —
    # the agency's shared feed.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # A phone number is PII and does not belong in plaintext in a column an
    # analytics query might select.
    address_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    address_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # `server_default`, not just `default`: destinations are also written by
    # raw SQL from webhook handlers holding no ORM object (mirroring
    # `sync_events`), where a Python-side default never runs.
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    __table_args__ = (
        # Per tenant, not global. The same recruiter's number can legitimately
        # appear under two agencies, and a global constraint would make the
        # second link fail with nothing to explain it.
        UniqueConstraint(
            "tenant_id", "channel", "address_hash", name="uq_destination_address"
        ),
        # A paired device always has an owner. `user_id IS NULL` is how every
        # other channel says "the agency's shared feed", and that reading is
        # incoherent here: the send path opens the socket belonging to
        # `user_id`, and the settings API hides a linked destination from
        # everyone but its owner. An ownerless row would therefore be a
        # destination nobody can see and nothing can send — so the database
        # refuses it rather than trusting each write path to remember.
        CheckConstraint(
            f"channel <> '{CHANNEL_WHATSAPP_LINKED}' OR user_id IS NOT NULL",
            name="ck_destination_linked_has_owner",
        ),
    )


class NotificationSubscription(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """Which events reach which destination — the event-by-channel matrix.

    There is no second representation of this. What the settings screen shows
    is a read of these rows.
    """

    __tablename__ = "notification_subscriptions"

    destination_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("notification_destinations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("destination_id", "event_kind", name="uq_subscription_event"),
    )


class NotificationLinkToken(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """Proof that the person who asked for a destination owns it.

    Stored hashed and single-use. A token in the clear is a token that leaks
    from a database backup into someone else's job orders.
    """

    __tablename__ = "notification_link_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Only set for WhatsApp, where the code is sent to a number the user typed
    # and we must know which number the code was for.
    address_encrypted: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationDelivery(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """The outbox. See the module docstring for why it carries three jobs."""

    __tablename__ = "notification_deliveries"

    destination_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("notification_destinations.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    # The opportunity this is about. Nullable because a rollup message is about
    # a batch rather than any one row.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=STATUS_PENDING,
        server_default=text(f"'{STATUS_PENDING}'"),
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The rate-cap query: this destination's rows for one event kind in the
        # trailing hour. Without it the cap check is a scan of the tenant's
        # whole delivery history on every single send.
        Index(
            "ix_deliveries_dest_kind_created",
            "destination_id",
            "event_kind",
            "created_at",
        ),
        # The sweep's query: rows stuck pending or suppressed, oldest first.
        Index("ix_deliveries_status_created", "status", "created_at"),
        # Dedupe. Partial, because a rollup has a null subject and several
        # rollups to one destination are legitimate.
        Index(
            "ix_deliveries_dedupe",
            "destination_id",
            "event_kind",
            "subject_id",
            unique=True,
            postgresql_where=text("subject_id IS NOT NULL"),
        ),
    )


class WhatsAppSuppression(Base, UUIDPrimaryKey, Timestamps):
    """Someone who has opted out of our WhatsApp number. Deliberately global.

    Meta's opt-out and quality rating attach to the *phone number*, and we
    operate one shared number across every tenant. "This person opted out" is
    therefore a fact about our WABA, not about one agency. A tenant-scoped
    table structurally cannot express it: agency B would keep messaging someone
    who opted out through agency A, and Meta would count that against the
    number every tenant shares.

    **The absence of `tenant_id` here is a correctness requirement, and a bug
    on any other table in this schema.** It is written only by the WhatsApp
    webhook and read only by the send path. Its RLS policy is `USING (true)`,
    not because RLS is unnecessary but because `verify_rls_enforced()` refuses
    to boot on any readable table without a forced policy — so the permission
    is granted explicitly rather than by omission.
    """

    __tablename__ = "whatsapp_suppressions"

    address_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 'user_stop', 'undeliverable', 'quality_block' — why we stopped.
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
