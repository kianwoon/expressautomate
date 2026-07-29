"""WA gateway session state (spec 2026-07-29, §2–§3).

Two tables, and the split between them is the whole point.

Baileys' `AuthenticationState` is not one blob. It is `creds` — a single JSON
object rewritten on every `creds.update` — plus a signal key store addressed by
*category* and *id*, which Baileys mutates a handful of keys at a time on
nearly every event. Storing the lot as one column would mean read-modify-write
on every mutation, and two concurrent writes would silently drop whichever key
lost the race. `wa_session_keys` therefore holds **one row per key**, primary
key `(session_id, category, key_id)`, so a write touches only the keys it names
and structurally cannot erase a key it never mentioned. Plan §11 names losing a
key write as the riskiest failure in this build: the restored session decrypts
nothing, WhatsApp logs the device out, every recruiter re-pairs, and repeated
re-pairing is itself a ban signal.

`creds` lives in the same table under category `'creds'` with key_id `''`
rather than in a column on `wa_sessions`: one storage path, one encryption
path, one set of tests, and nothing that can drift between the two.

**Only the gateway can read these values.** `value_encrypted` is AES-256-GCM
ciphertext whose key (`WA_GATEWAY_ENCRYPTION_KEY`) is set on the `gateway`
Koyeb service alone. FastAPI owns these rows and cannot decrypt them, which is
deliberate — this is the same precedent as `address_encrypted` in
`notification.py`, taken one step further because signal keys *are* the
recruiter's WhatsApp identity.

Nothing here is ever returned to the browser except `status` and the QR string
(plan §5), and the QR never lands in the database at all.
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

# The state machine of plan §6: pairing → connected → reconnecting →
# disconnected | logged_out. CHECK-constrained in the migration so an
# unrecognised status cannot reach the UI's switch statement.
STATUS_PAIRING = "pairing"
STATUS_CONNECTED = "connected"
STATUS_RECONNECTING = "reconnecting"
STATUS_DISCONNECTED = "disconnected"
STATUS_LOGGED_OUT = "logged_out"

SESSION_STATUSES = (
    STATUS_PAIRING,
    STATUS_CONNECTED,
    STATUS_RECONNECTING,
    STATUS_DISCONNECTED,
    STATUS_LOGGED_OUT,
)

# `creds` is one object, not a keyed collection, so it needs a stable address in
# a table keyed by (category, key_id). Empty string rather than NULL because a
# NULL in a primary key is not allowed and a sentinel that participates in the
# key is what makes the upsert work.
CREDS_CATEGORY = "creds"
CREDS_KEY_ID = ""


class WaSession(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One recruiter's paired WhatsApp device."""

    __tablename__ = "wa_sessions"

    # One session per recruiter, globally unique rather than per-tenant: a
    # WhatsApp device pairs to one process, and the gateway's advisory lock
    # (plan §2) is taken on user_id. Two rows for one user would let two
    # sockets fight over the same device and log both out.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # The paired number, known only after pairing succeeds.
    phone_e164: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=STATUS_PAIRING,
        server_default=text(f"'{STATUS_PAIRING}'"),
    )
    # Why, in words, for the UI to show verbatim — "Pairing timed out", the
    # disconnect reason Baileys reported. §15: we say what happened, we do not
    # invent a friendlier story.
    status_detail: Mapped[str | None] = mapped_column(Text)
    # The QR string itself is deliberately NOT stored (plan §5); only when the
    # in-process one expires, so the UI can stop showing a stale code.
    qr_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Daily cap (plan §9). Counter plus the date it counts, so the reset is
    # "sent_date <> today" rather than a scheduled job that can fail to run.
    sent_today: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    sent_date: Mapped[date | None] = mapped_column(Date)

    # Plan §9: the recruiter ticks a box acknowledging the ban risk before the
    # first pairing. Recorded, not assumed.
    ban_risk_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # P5: the jittered spacing deadline (plan §9) the *last admitted send*
    # set. Written once, when a send is admitted; every refusal in between
    # reads it and never rewrites it — a recruiter who waits exactly the
    # `retry_after_seconds` a refusal quoted must succeed on retry, and
    # re-rolling the jitter on each refusal would make that quote a lie.
    # NULL means no send has ever been admitted, so the first send always
    # clears this check.
    next_send_allowed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in SESSION_STATUSES) + ")",
            name="ck_wa_sessions_status",
        ),
        # Target of the composite FK below: the child rows can only reference a
        # session inside their own tenant.
        UniqueConstraint("tenant_id", "id", name="uq_wa_sessions_tenant_id_id"),
    )


class WaSessionKey(Base, TenantScoped, Timestamps):
    """One Baileys auth-state value: `creds`, or one signal key.

    No surrogate id. The primary key IS `(session_id, category, key_id)`,
    because that is exactly the address Baileys asks for and it is what makes
    the store's write an `ON CONFLICT … DO UPDATE` on a single row. A surrogate
    key would permit two rows for one Baileys key, and the reader would then
    have to pick — silently, wrongly, half the time.
    """

    __tablename__ = "wa_session_keys"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, nullable=False
    )
    # Baileys' `SignalDataTypeMap` keys ('pre-key', 'session', 'sender-key',
    # 'app-state-sync-key', …) plus 'creds'. Deliberately unconstrained text:
    # the categories are Baileys' vocabulary, not ours, and a CHECK here would
    # turn a Baileys upgrade that adds one into a write failure that logs every
    # recruiter out. The gateway is the only writer.
    category: Mapped[str] = mapped_column(String(48), primary_key=True, nullable=False)
    key_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    # AES-256-GCM: version byte ‖ 12-byte IV ‖ 16-byte tag ‖ ciphertext, with
    # AAD = "session_id:category:key_id" so a value moved to another row fails
    # to decrypt instead of impersonating that row's key. Layout documented in
    # gateway/src/crypto.ts, which is the only code that can read it.
    value_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (
        # Same-tenant composite FK, the idiom the rest of the schema uses: a key
        # row cannot be attached to another agency's session even if a caller
        # forges the id.
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["wa_sessions.tenant_id", "wa_sessions.id"],
            name="fk_wa_session_keys_session_same_tenant",
            ondelete="CASCADE",
        ),
        # Restore reads every key for a session in one query.
        Index("ix_wa_session_keys_session_id", "session_id"),
    )
