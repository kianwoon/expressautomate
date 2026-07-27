# Notification System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push "new job order" and "needs review" events to each recruiter's Telegram or WhatsApp, with per-user and per-tenant control over which events go where.

**Architecture:** Producers call `emit()` and know nothing about channels. `emit` writes outbox rows (`notification_deliveries`) inside the caller's tenant transaction, then fail-soft enqueues one arq job per row. A periodic sweep recovers rows whose enqueue was lost and flushes rate-capped rollups. Channels sit behind a `Channel` protocol so tests never touch the network.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Postgres 16 with RLS, arq + Redis, httpx, pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-07-28-notification-system-design.md](../specs/2026-07-28-notification-system-design.md)

## Global Constraints

- **No hardcoded values.** Every URL, template name, limit, and key comes from the repo-root `.env` via `app.core.config.settings`. A literal in source is a plan failure. The only exemption is SQL text and operator-facing diagnostic strings, which existing code marks with a `# allow-hardcode:` comment stating why.
- **Every business table carries `tenant_id`** via the `TenantScoped` mixin (plan §18), with a forced RLS policy created in the migration.
- **`verify_rls_enforced()` blocks startup** on any table the app role can `SELECT` that lacks both `relrowsecurity` and `relforcerowsecurity`. This applies to `whatsapp_suppressions` too, which is not tenant-scoped — it gets RLS enabled and forced with a deliberately permissive `USING (true)` policy. Enabling without forcing is not enough.
- **Single file ≤ 1500 lines.**
- **Tests never touch the network and never run against a remote database.** `tests/conftest.py` aborts collection if `DATABASE_URL` or `DATABASE_ADMIN_URL` names a non-local host.
- **Every arq job takes `tenant_id` as a keyword argument** and opens `tenant_session(uuid.UUID(tenant_id))`. Workers have no ambient tenant.
- **Every job name enqueued must appear in `WorkerSettings.functions`** in `app/workers/settings.py`. A missing name fails inside arq, past the queue, where the producer already saw success.
- **All API routes live under `/api`.** `tests/test_routing.py` fails if a route escapes it.
- Commands run from `backend/`. Test: `uv run pytest`. Lint: `uv run ruff check .`.

---

## File Structure

**Create:**

| Path | Responsibility |
|---|---|
| `app/models/notification.py` | Five SQLAlchemy models |
| `app/services/notify/__init__.py` | Re-exports `emit` |
| `app/services/notify/events.py` | Event-kind constants, payload dataclasses. No I/O |
| `app/services/notify/render.py` | Payload → per-channel content |
| `app/services/notify/channels/__init__.py` | Channel registry |
| `app/services/notify/channels/base.py` | `Channel` protocol, `SendResult`, `SendOutcome` |
| `app/services/notify/channels/telegram.py` | Telegram Bot API client |
| `app/services/notify/channels/whatsapp.py` | Meta Cloud API client |
| `app/services/notify/dispatch.py` | `emit()` — subscribers → outbox rows → enqueue |
| `app/services/notify/linking.py` | Issue and redeem verification tokens |
| `app/api/notifications.py` | Preference CRUD and linking endpoints |
| `app/api/telegram_webhook.py` | Telegram bot updates |
| `app/api/whatsapp_webhook.py` | Meta delivery statuses and opt-outs |
| `alembic/versions/20260728_1000_notifications.py` | Five tables, RLS, triggers |

**Modify:** `app/core/config.py`, `app/models/__init__.py`, `app/workers/jobs.py`, `app/workers/settings.py`, `app/workers/tasks.py`, `app/workers/main.py`, `app/main.py`, `app/services/ingest/persist.py`, `.env.example`.

---

## Task 1: Configuration

**Files:**
- Modify: `app/core/config.py`
- Modify: `.env.example`
- Test: `tests/test_notify_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `settings.TELEGRAM_BOT_TOKEN`, `settings.TELEGRAM_API_BASE_URL`, `settings.TELEGRAM_WEBHOOK_SECRET`, `settings.WHATSAPP_ACCESS_TOKEN`, `settings.WHATSAPP_PHONE_NUMBER_ID`, `settings.WHATSAPP_API_BASE_URL`, `settings.WHATSAPP_APP_SECRET`, `settings.WHATSAPP_VERIFY_TOKEN`, `settings.WHATSAPP_TEMPLATE_OPPORTUNITY_NEW`, `settings.WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW`, `settings.WHATSAPP_TEMPLATE_LINK_CODE`, `settings.WHATSAPP_TEMPLATE_LANG`, `settings.NOTIFY_RATE_CAP_PER_HOUR`, `settings.NOTIFY_LINK_TOKEN_TTL_MINUTES`, `settings.NOTIFY_MAX_ATTEMPTS`, `settings.NOTIFY_MAX_FAILURES`, `settings.NOTIFY_OPT_IN_MAX_PER_HOUR`, `settings.NOTIFY_SWEEP_INTERVAL_SECONDS`, `settings.NOTIFY_DELIVERY_STALE_MINUTES`, `settings.telegram_configured() -> bool`, `settings.whatsapp_configured() -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_config.py`:

```python
"""Notification settings load from the environment, never from source."""

from app.core.config import Settings, settings


def test_notify_defaults_are_present() -> None:
    assert settings.NOTIFY_RATE_CAP_PER_HOUR > 0
    assert settings.NOTIFY_LINK_TOKEN_TTL_MINUTES > 0
    assert settings.NOTIFY_MAX_ATTEMPTS > 0
    assert settings.NOTIFY_MAX_FAILURES > 0
    assert settings.NOTIFY_OPT_IN_MAX_PER_HOUR > 0


def test_channels_report_unconfigured_when_credentials_are_absent() -> None:
    """An empty token must read as 'not configured', not as a usable client."""
    blank = Settings(
        APP_SECRET_KEY="x",
        TOKEN_ENCRYPTION_KEY="x",
        FRONTEND_ORIGIN="http://localhost:3000",
        DATABASE_URL="postgresql://u:p@localhost/db",
        TELEGRAM_BOT_TOKEN="",
        WHATSAPP_ACCESS_TOKEN="",
        WHATSAPP_PHONE_NUMBER_ID="",
    )
    assert blank.telegram_configured() is False
    assert blank.whatsapp_configured() is False


def test_channels_report_configured_when_credentials_are_present() -> None:
    ready = Settings(
        APP_SECRET_KEY="x",
        TOKEN_ENCRYPTION_KEY="x",
        FRONTEND_ORIGIN="http://localhost:3000",
        DATABASE_URL="postgresql://u:p@localhost/db",
        TELEGRAM_BOT_TOKEN="bot-token",
        TELEGRAM_API_BASE_URL="https://api.telegram.org",
        WHATSAPP_ACCESS_TOKEN="wa-token",
        WHATSAPP_PHONE_NUMBER_ID="1234567890",
        WHATSAPP_API_BASE_URL="https://graph.facebook.com/v21.0",
    )
    assert ready.telegram_configured() is True
    assert ready.whatsapp_configured() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notify_config.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'NOTIFY_RATE_CAP_PER_HOUR'`

- [ ] **Step 3: Add the settings**

In `app/core/config.py`, add inside `class Settings(BaseSettings)`, after the existing `ARQ_MAX_TRIES` line:

```python
    # --- Notifications (spec 2026-07-28) ---
    # Blank by default. A channel with no credentials is *skipped*, not an
    # error: the platform must boot and ingest mail before either provider is
    # provisioned, and a missing token discovered at startup is far cheaper
    # than one discovered inside a worker on the far side of the queue.
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_API_BASE_URL: str = ""
    # Telegram echoes this in `X-Telegram-Bot-Api-Secret-Token`. Without it the
    # webhook accepts anything that can reach the URL, and the URL is public.
    TELEGRAM_WEBHOOK_SECRET: str = ""

    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_API_BASE_URL: str = ""
    # Meta signs webhook bodies with this; it is the app secret, not the token.
    WHATSAPP_APP_SECRET: str = ""
    # Echoed back during Meta's one-time webhook verification handshake.
    WHATSAPP_VERIFY_TOKEN: str = ""

    # Template *names*, not bodies. Meta's approval cycle can rename or
    # re-version a template with no deploy on our side; a name compiled into
    # source would need one, and the failure is a silent non-delivery.
    WHATSAPP_TEMPLATE_OPPORTUNITY_NEW: str = ""
    WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW: str = ""
    WHATSAPP_TEMPLATE_LINK_CODE: str = ""
    WHATSAPP_TEMPLATE_LANG: str = "en"

    # A forty-vacancy morning is forty billable WhatsApp messages otherwise.
    NOTIFY_RATE_CAP_PER_HOUR: int = Field(default=6, gt=0)
    NOTIFY_LINK_TOKEN_TTL_MINUTES: int = Field(default=15, gt=0)
    NOTIFY_MAX_ATTEMPTS: int = Field(default=5, gt=0)
    NOTIFY_MAX_FAILURES: int = Field(default=3, gt=0)
    # Sending an authentication template to any number a user types is an
    # OTP pump aimed at our WABA's reputation. This is the ceiling per user.
    NOTIFY_OPT_IN_MAX_PER_HOUR: int = Field(default=5, gt=0)
    NOTIFY_SWEEP_INTERVAL_SECONDS: float = Field(default=300.0, gt=0)
    # How long a delivery may sit `pending` before the sweep assumes its
    # enqueue was lost. Must exceed the worst realistic queue latency, or the
    # sweep competes with a job that is merely slow.
    NOTIFY_DELIVERY_STALE_MINUTES: int = Field(default=10, gt=0)
```

Then add these two methods to `Settings`, beside the existing `graph_configured`:

```python
    def telegram_configured(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_API_BASE_URL)

    def whatsapp_configured(self) -> bool:
        return bool(
            self.WHATSAPP_ACCESS_TOKEN
            and self.WHATSAPP_PHONE_NUMBER_ID
            and self.WHATSAPP_API_BASE_URL
        )
```

- [ ] **Step 4: Document the keys**

Append to `.env.example`:

```
# --- Notifications ---
TELEGRAM_BOT_TOKEN=
TELEGRAM_API_BASE_URL=https://api.telegram.org
TELEGRAM_WEBHOOK_SECRET=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_API_BASE_URL=https://graph.facebook.com/v21.0
WHATSAPP_APP_SECRET=
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_TEMPLATE_OPPORTUNITY_NEW=
WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW=
WHATSAPP_TEMPLATE_LINK_CODE=
WHATSAPP_TEMPLATE_LANG=en
NOTIFY_RATE_CAP_PER_HOUR=6
NOTIFY_LINK_TOKEN_TTL_MINUTES=15
NOTIFY_MAX_ATTEMPTS=5
NOTIFY_MAX_FAILURES=3
NOTIFY_OPT_IN_MAX_PER_HOUR=5
NOTIFY_SWEEP_INTERVAL_SECONDS=300
NOTIFY_DELIVERY_STALE_MINUTES=10
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_notify_config.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add app/core/config.py .env.example tests/test_notify_config.py
git commit -m "Let the operator name the templates Meta may rename"
```

---

## Task 2: Models and migration

**Files:**
- Create: `app/models/notification.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/20260728_1000_notifications.py`
- Test: `tests/test_notification_schema.py`

**Interfaces:**
- Consumes: `settings` (Task 1)
- Produces: `NotificationDestination`, `NotificationSubscription`, `NotificationLinkToken`, `NotificationDelivery`, `WhatsAppSuppression` models; constants `CHANNEL_TELEGRAM = "telegram"`, `CHANNEL_WHATSAPP = "whatsapp"`, `STATUS_PENDING = "pending"`, `STATUS_SENDING = "sending"`, `STATUS_SENT = "sent"`, `STATUS_FAILED = "failed"`, `STATUS_SUPPRESSED = "suppressed"`; helper `address_digest(address: str) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notification_schema.py`:

```python
"""Schema guarantees the RLS policy would otherwise hide behind empty results.

These use `admin_session` (the schema owner, which bypasses RLS) because a
constraint violation and a policy-filtered read are indistinguishable from the
application role.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.notification import address_digest


def test_address_digest_is_stable_and_not_reversible() -> None:
    first = address_digest("+6591234567")
    assert first == address_digest("+6591234567")
    assert first != address_digest("+6591234568")
    assert "6591234567" not in first


@pytest.fixture
async def tenant_pair(admin_session):
    a, b = uuid.uuid4(), uuid.uuid4()
    for tid, name in ((a, "agency-a"), (b, "agency-b")):
        await admin_session.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": tid, "name": name},
        )
    await admin_session.commit()
    yield a, b
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [a, b]}
    )
    await admin_session.commit()


async def test_same_address_may_exist_in_two_tenants(admin_session, tenant_pair) -> None:
    """One recruiter can work for two agencies. A global unique index would
    make the second link fail with nothing to explain it."""
    a, b = tenant_pair
    digest = address_digest("+6591234567")
    for tid in (a, b):
        await admin_session.execute(
            text(
                "INSERT INTO notification_destinations "
                "(id, tenant_id, channel, address_encrypted, address_hash) "
                "VALUES (:id, :tid, 'whatsapp', 'ciphertext', :hash)"
            ),
            {"id": uuid.uuid4(), "tid": tid, "hash": digest},
        )
    await admin_session.commit()


async def test_same_address_twice_in_one_tenant_is_rejected(
    admin_session, tenant_pair
) -> None:
    a, _ = tenant_pair
    digest = address_digest("+6599999999")
    for _ in range(2):
        await admin_session.execute(
            text(
                "INSERT INTO notification_destinations "
                "(id, tenant_id, channel, address_encrypted, address_hash) "
                "VALUES (:id, :tid, 'whatsapp', 'ciphertext', :hash)"
            ),
            {"id": uuid.uuid4(), "tid": a, "hash": digest},
        )
    with pytest.raises(IntegrityError):
        await admin_session.commit()


async def test_suppressions_table_is_readable_and_forced(admin_session) -> None:
    """Not tenant-scoped, but still FORCE RLS — `verify_rls_enforced` refuses
    to boot otherwise, and the send path must be able to read it."""
    row = (
        await admin_session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'whatsapp_suppressions'"
            )
        )
    ).one()
    assert row == (True, True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notification_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.models.notification'`

- [ ] **Step 3: Write the models**

Create `app/models/notification.py`:

```python
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

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

CHANNEL_TELEGRAM = "telegram"
CHANNEL_WHATSAPP = "whatsapp"

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
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        # Per tenant, not global. The same recruiter's number can legitimately
        # appear under two agencies, and a global constraint would make the
        # second link fail with nothing to explain it.
        UniqueConstraint(
            "tenant_id", "channel", "address_hash", name="uq_destination_address"
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
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

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
        String(16), nullable=False, default=STATUS_PENDING
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
            postgresql_where=text_subject_not_null := None,  # replaced below
        ),
    )


# The partial-index predicate cannot be written inline above without importing
# `text` into the class body, which reads worse than fixing it here.
from sqlalchemy import text as _sql_text  # noqa: E402

NotificationDelivery.__table__.indexes  # noqa: B018  (touch, so the loop below sees them)
for _index in NotificationDelivery.__table__.indexes:
    if _index.name == "ix_deliveries_dedupe":
        _index.dialect_options["postgresql"]["where"] = _sql_text(
            "subject_id IS NOT NULL"
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
```

> **Note for the implementer:** the partial-index workaround at the bottom of the file is ugly. If SQLAlchemy accepts `postgresql_where=text("subject_id IS NOT NULL")` directly in `__table_args__` in this version, use that and delete the loop. Verify with `uv run python -c "from app.models.notification import NotificationDelivery"` and check the index renders.

- [ ] **Step 4: Register the models**

In `app/models/__init__.py`, add the import (keeping alphabetical order) and the `__all__` entries:

```python
from app.models.notification import (
    NotificationDelivery,
    NotificationDestination,
    NotificationLinkToken,
    NotificationSubscription,
    WhatsAppSuppression,
)
```

Add `"NotificationDelivery"`, `"NotificationDestination"`, `"NotificationLinkToken"`, `"NotificationSubscription"`, and `"WhatsAppSuppression"` to `__all__`.

- [ ] **Step 5: Generate the migration**

Run: `uv run alembic revision --autogenerate -m "notification destinations, subscriptions and outbox"`

Rename the generated file to `alembic/versions/20260728_1000_notifications.py`. Autogenerate produces the tables and indexes but knows nothing about RLS — add the policy block below, following `20260727_2200_sync_events.py` verbatim:

```python
# Same list-and-loop as the sync_events migration, for the same reason:
# `verify_rls_enforced()` refuses to boot if a readable table has no forced
# policy, so a table added to the models and forgotten here stops the deploy
# rather than quietly serving every agency's rows.
PROTECTED: list[tuple[str, str]] = [
    ("notification_destinations", "tenant_id"),
    ("notification_subscriptions", "tenant_id"),
    ("notification_link_tokens", "tenant_id"),
    ("notification_deliveries", "tenant_id"),
]

# Not tenant-scoped, and that is the point — see the model docstring. It still
# needs a forced policy, because the startup check is structural and will
# refuse to boot on any readable table without one.
GLOBAL_TABLES: list[str] = ["whatsapp_suppressions"]

SETTING = "app.tenant_id"


def _enforce_rls() -> None:
    for table, column in PROTECTED:
        # nullif is load-bearing: once set_config has run on a connection the
        # setting stays defined for that backend, so an unscoped transaction
        # reads back '' rather than NULL, and casting '' to uuid raises
        # instead of matching nothing.
        predicate = f"{column} = nullif(current_setting('{SETTING}', true), '')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )

    for table in GLOBAL_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS global_read ON {table}")
        op.execute(f"CREATE POLICY global_read ON {table} USING (true) WITH CHECK (true)")


def _touch_updated_at() -> None:
    """Bind the existing trigger — these rows are written as raw SQL by
    workers holding no ORM object, so `Timestamps.updated_at`'s ORM-side
    `onupdate` never fires."""
    for table in [t for t, _ in PROTECTED] + GLOBAL_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_touch_updated_at ON {table}")
        op.execute(
            f"""
            CREATE TRIGGER {table}_touch_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
            """
        )
```

Call `_enforce_rls()` and `_touch_updated_at()` at the end of `upgrade()`.

- [ ] **Step 6: Apply the migration**

Run: `uv run alembic upgrade head`
Expected: no error, ending in `Running upgrade ... -> <rev>, notification destinations, subscriptions and outbox`

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_notification_schema.py tests/test_rls.py -v`
Expected: all pass. `test_rls.py` matters here — it exercises `verify_rls_enforced()`, which is what catches a table added to the models and forgotten in the migration.

- [ ] **Step 8: Commit**

```bash
git add app/models/notification.py app/models/__init__.py alembic/versions/20260728_1000_notifications.py tests/test_notification_schema.py
git commit -m "Give the outbox somewhere to live"
```

---

## Task 3: Events and rendering

**Files:**
- Create: `app/services/notify/__init__.py`
- Create: `app/services/notify/events.py`
- Create: `app/services/notify/render.py`
- Test: `tests/test_notify_render.py`

**Interfaces:**
- Consumes: `settings` (Task 1)
- Produces:
  - `EVENT_OPPORTUNITY_NEW = "opportunity.new"`, `EVENT_OPPORTUNITY_NEEDS_REVIEW = "opportunity.needs_review"`, `ALL_EVENT_KINDS: tuple[str, ...]`
  - `@dataclass(frozen=True) class OpportunityEvent: kind: str; tenant_id: uuid.UUID; opportunity_id: uuid.UUID; job_title: str | None; company_name: str | None; location: str | None; salary: str | None`
  - `@dataclass(frozen=True) class TelegramContent: text: str`
  - `@dataclass(frozen=True) class WhatsAppContent: template_name: str; language: str; body_params: list[str]; button_param: str`
  - `render(event: OpportunityEvent, channel: str, rollup: int = 0) -> TelegramContent | WhatsAppContent`
  - `MISSING = "Not mentioned"`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_render.py`:

```python
"""Rendering is pure — no database, no network, no clock."""

import uuid

import pytest

from app.core.config import settings
from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP
from app.services.notify.events import (
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_NEW,
    MISSING,
    OpportunityEvent,
)
from app.services.notify.render import render


def _event(**overrides) -> OpportunityEvent:
    base = {
        "kind": EVENT_OPPORTUNITY_NEW,
        "tenant_id": uuid.uuid4(),
        "opportunity_id": uuid.uuid4(),
        "job_title": "Senior Backend Engineer",
        "company_name": "Acme Pte Ltd",
        "location": "Singapore",
        "salary": "SGD 8,000 - 10,000 monthly",
    }
    return OpportunityEvent(**{**base, **overrides})


def test_telegram_names_the_job_and_the_company() -> None:
    content = render(_event(), CHANNEL_TELEGRAM)
    assert "Senior Backend Engineer" in content.text
    assert "Acme Pte Ltd" in content.text


def test_telegram_says_not_mentioned_rather_than_inventing() -> None:
    """Plan section 15: an absent value is stated as absent."""
    content = render(_event(salary=None), CHANNEL_TELEGRAM)
    assert MISSING in content.text


def test_whatsapp_params_are_ordered_title_company_location_salary() -> None:
    """A swapped {{1}}/{{2}} reads as a job title at a company that does not
    exist, and Meta will deliver it happily."""
    content = render(_event(), CHANNEL_WHATSAPP)
    assert content.body_params == [
        "Senior Backend Engineer",
        "Acme Pte Ltd",
        "Singapore",
        "SGD 8,000 - 10,000 monthly",
    ]


def test_whatsapp_button_param_is_the_opportunity_id() -> None:
    event = _event()
    content = render(event, CHANNEL_WHATSAPP)
    assert content.button_param == str(event.opportunity_id)


def test_whatsapp_template_comes_from_config_not_source() -> None:
    new = render(_event(kind=EVENT_OPPORTUNITY_NEW), CHANNEL_WHATSAPP)
    review = render(
        _event(kind=EVENT_OPPORTUNITY_NEEDS_REVIEW), CHANNEL_WHATSAPP
    )
    assert new.template_name == settings.WHATSAPP_TEMPLATE_OPPORTUNITY_NEW
    assert review.template_name == settings.WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW
    assert new.language == settings.WHATSAPP_TEMPLATE_LANG


def test_whatsapp_never_emits_an_empty_param() -> None:
    """Meta rejects a template whose parameter is an empty string, and the
    rejection arrives as a failed send with no obvious cause."""
    content = render(
        _event(job_title=None, company_name=None, location=None, salary=None),
        CHANNEL_WHATSAPP,
    )
    assert all(p for p in content.body_params)


def test_rollup_is_appended_to_telegram() -> None:
    content = render(_event(), CHANNEL_TELEGRAM, rollup=4)
    assert "4 more" in content.text


def test_rollup_does_not_change_whatsapp_param_count() -> None:
    """The template is approved with a fixed parameter count; adding one for a
    rollup would make every capped send fail."""
    plain = render(_event(), CHANNEL_WHATSAPP)
    rolled = render(_event(), CHANNEL_WHATSAPP, rollup=4)
    assert len(plain.body_params) == len(rolled.body_params)


def test_unknown_channel_is_an_error() -> None:
    with pytest.raises(ValueError):
        render(_event(), "carrier-pigeon")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notify_render.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.notify'`

- [ ] **Step 3: Write the events module**

Create `app/services/notify/__init__.py`:

```python
"""Notification delivery (spec 2026-07-28)."""
```

Create `app/services/notify/events.py`:

```python
"""What happened, in a form that has not yet chosen a channel.

Constants rather than free strings at the call sites, for the same reason
`sync_event.py` gives: subscriptions are stored on this value, so a typo in one
producer becomes a category nobody is subscribed to rather than an error.
"""

import uuid
from dataclasses import dataclass

EVENT_OPPORTUNITY_NEW = "opportunity.new"
EVENT_OPPORTUNITY_NEEDS_REVIEW = "opportunity.needs_review"

ALL_EVENT_KINDS: tuple[str, ...] = (
    EVENT_OPPORTUNITY_NEW,
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
)

# What an absent value reads as. The AI must not fabricate one (plan §15), and
# a blank in a WhatsApp template parameter is rejected by Meta outright.
MISSING = "Not mentioned"


@dataclass(frozen=True)
class OpportunityEvent:
    """One vacancy, denormalised at emit time.

    The fields are copied rather than looked up later on purpose: by the time
    the delivery job runs, the opportunity may have been edited or deleted, and
    a notification should describe what happened when it happened.
    """

    kind: str
    tenant_id: uuid.UUID
    opportunity_id: uuid.UUID
    job_title: str | None
    company_name: str | None
    location: str | None
    salary: str | None
```

- [ ] **Step 4: Write the renderer**

Create `app/services/notify/render.py`:

```python
"""One event, two very different shapes.

Telegram takes free-form text. WhatsApp does not: every message here is
business-initiated outside any 24-hour customer service window, which under
Meta's per-message pricing means a pre-approved *utility* template — ordered
positional parameters, no prose. The two renderers are genuinely different and
this module does not pretend otherwise by inventing a shared abstraction that
would fit neither.
"""

from dataclasses import dataclass

from app.core.config import settings
from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP
from app.services.notify.events import (
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_NEW,
    MISSING,
    OpportunityEvent,
)


@dataclass(frozen=True)
class TelegramContent:
    text: str


@dataclass(frozen=True)
class WhatsAppContent:
    template_name: str
    language: str
    body_params: list[str]
    button_param: str


_TEMPLATE_FOR = {
    EVENT_OPPORTUNITY_NEW: lambda: settings.WHATSAPP_TEMPLATE_OPPORTUNITY_NEW,
    EVENT_OPPORTUNITY_NEEDS_REVIEW: lambda: settings.WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW,
}

# allow-hardcode: user-facing copy, not matching logic.
_HEADLINE = {
    EVENT_OPPORTUNITY_NEW: "New job order",
    EVENT_OPPORTUNITY_NEEDS_REVIEW: "Job order needs review",
}


def _or_missing(value: str | None) -> str:
    return value if value else MISSING


def render(
    event: OpportunityEvent, channel: str, rollup: int = 0
) -> TelegramContent | WhatsAppContent:
    """Content for one event on one channel.

    `rollup` is the count of sends suppressed by the rate cap since the last
    delivery. It is mentioned only on Telegram: the WhatsApp template is
    approved with a fixed parameter count, so adding one would make every
    capped send fail — which is the send that matters most.
    """
    if channel == CHANNEL_TELEGRAM:
        return _telegram(event, rollup)
    if channel == CHANNEL_WHATSAPP:
        return _whatsapp(event)
    raise ValueError(f"Unknown notification channel: {channel!r}")


def _telegram(event: OpportunityEvent, rollup: int) -> TelegramContent:
    lines = [
        f"*{_HEADLINE[event.kind]}*",
        f"{_or_missing(event.job_title)} — {_or_missing(event.company_name)}",
        f"Location: {_or_missing(event.location)}",
        f"Salary: {_or_missing(event.salary)}",
    ]
    if rollup:
        lines.append(f"_and {rollup} more while notifications were rate-limited_")
    return TelegramContent(text="\n".join(lines))


def _whatsapp(event: OpportunityEvent) -> WhatsAppContent:
    return WhatsAppContent(
        template_name=_TEMPLATE_FOR[event.kind](),
        language=settings.WHATSAPP_TEMPLATE_LANG,
        # Order is the contract with the approved template. Changing it here
        # without resubmitting the template produces a delivered message that
        # reads as a job title at a company that does not exist.
        body_params=[
            _or_missing(event.job_title),
            _or_missing(event.company_name),
            _or_missing(event.location),
            _or_missing(event.salary),
        ],
        button_param=str(event.opportunity_id),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_notify_render.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add app/services/notify/ tests/test_notify_render.py
git commit -m "Say 'Not mentioned' rather than send an empty template slot"
```

---

## Task 4: Channel clients

**Files:**
- Create: `app/services/notify/channels/__init__.py`
- Create: `app/services/notify/channels/base.py`
- Create: `app/services/notify/channels/telegram.py`
- Create: `app/services/notify/channels/whatsapp.py`
- Test: `tests/test_notify_channels.py`

**Interfaces:**
- Consumes: `TelegramContent`, `WhatsAppContent` (Task 3), `settings` (Task 1)
- Produces:
  - `class SendOutcome(StrEnum): SENT; TRANSIENT; PERMANENT`
  - `@dataclass(frozen=True) class SendResult: outcome: SendOutcome; provider_message_id: str | None = None; error: str | None = None; retry_after: float | None = None`
  - `class Channel(Protocol): async def send(self, address: str, content) -> SendResult`
  - `TelegramChannel()`, `WhatsAppChannel()` — both take an optional `client: httpx.AsyncClient | None` constructor argument, which is the test seam
  - `channel_for(name: str) -> Channel`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_channels.py`:

```python
"""Channel clients, against a stub transport. Nothing here touches the network."""

import httpx
import pytest

from app.core.config import settings
from app.services.notify.channels.base import SendOutcome
from app.services.notify.channels.telegram import TelegramChannel
from app.services.notify.channels.whatsapp import WhatsAppChannel
from app.services.notify.render import TelegramContent, WhatsAppContent


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _wa_content() -> WhatsAppContent:
    return WhatsAppContent(
        template_name="opportunity_new",
        language="en",
        body_params=["Engineer", "Acme", "Singapore", "SGD 8,000"],
        button_param="11111111-1111-1111-1111-111111111111",
    )


async def test_telegram_success_returns_the_message_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.SENT
    assert result.provider_message_id == "42"


async def test_telegram_403_is_permanent() -> None:
    """The recruiter blocked the bot. Retrying cannot change that."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "bot was blocked"})

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.PERMANENT


async def test_telegram_429_is_transient_and_carries_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "17"},
            json={"ok": False, "description": "Too Many Requests"},
        )

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.TRANSIENT
    assert result.retry_after == 17.0


async def test_telegram_500_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream is unwell")

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.TRANSIENT


async def test_whatsapp_posts_a_template_with_ordered_params() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    result = await WhatsAppChannel(client=_client(handler)).send(
        "+6591234567", _wa_content()
    )
    assert result.outcome is SendOutcome.SENT
    assert result.provider_message_id == "wamid.ABC"
    assert seen["type"] == "template"
    assert seen["template"]["name"] == "opportunity_new"
    body = next(c for c in seen["template"]["components"] if c["type"] == "body")
    assert [p["text"] for p in body["parameters"]] == [
        "Engineer",
        "Acme",
        "Singapore",
        "SGD 8,000",
    ]


async def test_whatsapp_131026_is_permanent() -> None:
    """Undeliverable — the number is not on WhatsApp. Never retry it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 131026, "message": "undeliverable"}})

    result = await WhatsAppChannel(client=_client(handler)).send(
        "+6591234567", _wa_content()
    )
    assert result.outcome is SendOutcome.PERMANENT


async def test_whatsapp_rate_limit_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 130429, "message": "rate limit"}})

    result = await WhatsAppChannel(client=_client(handler)).send(
        "+6591234567", _wa_content()
    )
    assert result.outcome is SendOutcome.TRANSIENT


async def test_network_failure_is_transient_not_a_crash() -> None:
    """A worker that raises here loses the claim and the row sticks in
    `sending` until the sweep finds it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.TRANSIENT


async def test_send_url_is_built_from_config() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    await WhatsAppChannel(client=_client(handler)).send("+6591234567", _wa_content())
    assert seen["url"].startswith(settings.WHATSAPP_API_BASE_URL)
    assert settings.WHATSAPP_PHONE_NUMBER_ID in seen["url"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notify_channels.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.notify.channels'`

- [ ] **Step 3: Write the protocol**

Create `app/services/notify/channels/base.py`:

```python
"""The shape every channel presents, and the three answers a send can give.

Three outcomes rather than a boolean, because the caller's next move differs
completely. TRANSIENT retries. PERMANENT must not — retrying a number that is
not on WhatsApp burns quota forever and never succeeds — and it disables the
destination so a dead address becomes visible instead of absorbing messages.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class SendOutcome(StrEnum):
    SENT = "sent"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    provider_message_id: str | None = None
    error: str | None = None
    # Seconds, from the provider's own Retry-After. Honouring it is what keeps
    # a throttled account from being throttled harder.
    retry_after: float | None = None


class Channel(Protocol):
    """What `deliver_notification` depends on — never a concrete client.

    Tests substitute a fake implementing this and no test touches the network,
    the same seam `workers/queue.py` uses for `_create_pool`.
    """

    async def send(self, address: str, content) -> SendResult: ...
```

Create `app/services/notify/channels/__init__.py`:

```python
"""Channel registry.

Imports are inside `channel_for` so a module-level import of this package does
not require either provider's configuration — the API process needs the
registry to validate a channel name long before it needs a client.
"""

from app.services.notify.channels.base import Channel, SendOutcome, SendResult

__all__ = ["Channel", "SendOutcome", "SendResult", "channel_for"]


def channel_for(name: str) -> Channel:
    from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP
    from app.services.notify.channels.telegram import TelegramChannel
    from app.services.notify.channels.whatsapp import WhatsAppChannel

    if name == CHANNEL_TELEGRAM:
        return TelegramChannel()
    if name == CHANNEL_WHATSAPP:
        return WhatsAppChannel()
    raise ValueError(f"Unknown notification channel: {name!r}")
```

- [ ] **Step 4: Write the Telegram client**

Create `app/services/notify/channels/telegram.py`:

```python
"""Telegram Bot API.

Telegram has no template regime and no 24-hour window, so this sends prose.
The whole client is one POST.
"""

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.notify.channels.base import SendOutcome, SendResult
from app.services.notify.render import TelegramContent

log = get_logger(__name__)

# Telegram's own retry hint lives in the body, not only in the header.
_RETRY_AFTER_BODY_KEY = "retry_after"


class TelegramChannel:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Injected in tests. Constructing one here by default keeps every
        # caller from having to know about transports.
        self._client = client
        self._owns_client = client is None

    async def send(self, address: str, content: TelegramContent) -> SendResult:
        url = f"{settings.TELEGRAM_API_BASE_URL}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": address,
            "text": content.text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # Never raise out of a channel. The caller has a claimed row to
            # release, and an exception here would leave it stuck in `sending`
            # until the sweep found it.
            return SendResult(outcome=SendOutcome.TRANSIENT, error=str(exc))
        finally:
            if self._owns_client:
                await client.aclose()

        return _interpret(response)


def _interpret(response: httpx.Response) -> SendResult:
    if response.status_code == 200:
        body = response.json()
        message_id = body.get("result", {}).get("message_id")
        return SendResult(
            outcome=SendOutcome.SENT,
            provider_message_id=str(message_id) if message_id is not None else None,
        )

    detail = _describe(response)

    if response.status_code == 429:
        return SendResult(
            outcome=SendOutcome.TRANSIENT, error=detail, retry_after=_retry_after(response)
        )
    if response.status_code >= 500:
        return SendResult(outcome=SendOutcome.TRANSIENT, error=detail)
    # 400 (chat not found), 403 (bot blocked). Both mean this destination will
    # never accept a message again; retrying is throughput spent on nothing.
    return SendResult(outcome=SendOutcome.PERMANENT, error=detail)


def _describe(response: httpx.Response) -> str:
    try:
        return str(response.json().get("description", response.text))[:500]
    except ValueError:
        return response.text[:500]


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        value = response.json().get("parameters", {}).get(_RETRY_AFTER_BODY_KEY)
        return float(value) if value is not None else None
    except (ValueError, AttributeError):
        return None
```

- [ ] **Step 5: Write the WhatsApp client**

Create `app/services/notify/channels/whatsapp.py`:

```python
"""Meta WhatsApp Cloud API.

Every message this sends is business-initiated outside any customer service
window, so it is always a pre-approved template — there is no free-form path
here and adding one would produce sends Meta rejects at the edge.
"""

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.notify.channels.base import SendOutcome, SendResult
from app.services.notify.render import WhatsAppContent

log = get_logger(__name__)

# Meta error codes that will never succeed on retry. Anything not listed is
# treated as transient, which is the safe default: a retried transient costs
# one extra call, a retried permanent costs every call forever.
PERMANENT_ERROR_CODES = frozenset(
    {
        131026,  # Message undeliverable — the number is not a WhatsApp user.
        131047,  # Re-engagement required; a template cannot open this window.
        131051,  # Unsupported message type.
        132000,  # Template param count does not match the approved template.
        132001,  # Template does not exist in this language.
        132015,  # Template is paused for quality reasons.
        132016,  # Template has been disabled.
        133010,  # Phone number not registered.
    }
)


class WhatsAppChannel:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def send(self, address: str, content: WhatsAppContent) -> SendResult:
        url = f"{settings.WHATSAPP_API_BASE_URL}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": address,
            "type": "template",
            "template": {
                "name": content.template_name,
                "language": {"code": content.language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": value}
                            for value in content.body_params
                        ],
                    },
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [{"type": "text", "text": content.button_param}],
                    },
                ],
            },
        }
        headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}

        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return SendResult(outcome=SendOutcome.TRANSIENT, error=str(exc))
        finally:
            if self._owns_client:
                await client.aclose()

        return _interpret(response)


def _interpret(response: httpx.Response) -> SendResult:
    if response.status_code == 200:
        body = response.json()
        messages = body.get("messages") or []
        return SendResult(
            outcome=SendOutcome.SENT,
            provider_message_id=messages[0].get("id") if messages else None,
        )

    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    code = error.get("code")
    detail = str(error.get("message", response.text))[:500]

    if code in PERMANENT_ERROR_CODES:
        return SendResult(outcome=SendOutcome.PERMANENT, error=detail)
    if response.status_code == 429 or response.status_code >= 500:
        return SendResult(
            outcome=SendOutcome.TRANSIENT, error=detail, retry_after=_retry_after(response)
        )
    # An unrecognised 4xx. Transient by default — see PERMANENT_ERROR_CODES.
    return SendResult(outcome=SendOutcome.TRANSIENT, error=detail)


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    try:
        return float(header) if header else None
    except ValueError:
        return None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_notify_channels.py -v`
Expected: 9 passed

- [ ] **Step 7: Commit**

```bash
git add app/services/notify/channels/ tests/test_notify_channels.py
git commit -m "Tell a blocked bot apart from a busy one"
```

---

## Task 5: Dispatch — subscribers, outbox, rate cap

**Files:**
- Create: `app/services/notify/dispatch.py`
- Modify: `app/services/notify/__init__.py`
- Test: `tests/test_notify_dispatch.py`

**Interfaces:**
- Consumes: models (Task 2), `OpportunityEvent` (Task 3), `enqueue` from `app.workers.queue`
- Produces:
  - `async def emit(event: OpportunityEvent, session: AsyncSession) -> list[uuid.UUID]` — writes outbox rows in the caller's transaction and returns their ids. Does **not** enqueue.
  - `async def enqueue_deliveries(tenant_id: uuid.UUID, delivery_ids: list[uuid.UUID]) -> int` — called after the caller commits.
  - `async def emit_and_enqueue(event: OpportunityEvent) -> int` — opens its own tenant session; for callers with no transaction of their own.
  - `async def rate_capped(session, destination_id: uuid.UUID, event_kind: str) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_dispatch.py`:

```python
"""Dispatch: who is subscribed, what lands in the outbox, and what the cap eats."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_TELEGRAM,
    STATUS_PENDING,
    STATUS_SUPPRESSED,
    address_digest,
)
from app.services.notify.dispatch import emit, rate_capped
from app.services.notify.events import EVENT_OPPORTUNITY_NEW, OpportunityEvent


@pytest.fixture
async def wired(admin_session):
    """One tenant, one user, one verified Telegram destination subscribed to
    EVENT_OPPORTUNITY_NEW."""
    tenant_id, user_id, dest_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'agency')"), {"id": tenant_id}
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email) "
            "VALUES (:id, :tid, 'r@agency.sg')"
        ),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'ciphertext', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_subscriptions "
            "(id, tenant_id, destination_id, event_kind, active) "
            "VALUES (:id, :tid, :did, :kind, true)"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tenant_id,
            "did": dest_id,
            "kind": EVENT_OPPORTUNITY_NEW,
        },
    )
    await admin_session.commit()
    yield tenant_id, user_id, dest_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def _event(tenant_id: uuid.UUID) -> OpportunityEvent:
    return OpportunityEvent(
        kind=EVENT_OPPORTUNITY_NEW,
        tenant_id=tenant_id,
        opportunity_id=uuid.uuid4(),
        job_title="Engineer",
        company_name="Acme",
        location="Singapore",
        salary="SGD 8,000",
    )


async def test_emit_writes_one_pending_row_per_subscriber(wired) -> None:
    tenant_id, _, dest_id = wired
    async with tenant_session(tenant_id) as session:
        ids = await emit(_event(tenant_id), session)
    assert len(ids) == 1

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT destination_id, status FROM notification_deliveries "
                    "WHERE id = :id"
                ),
                {"id": ids[0]},
            )
        ).one()
    assert row.destination_id == dest_id
    assert row.status == STATUS_PENDING


async def test_emit_ignores_an_inactive_subscription(wired, admin_session) -> None:
    tenant_id, _, dest_id = wired
    await admin_session.execute(
        text("UPDATE notification_subscriptions SET active = false WHERE destination_id = :d"),
        {"d": dest_id},
    )
    await admin_session.commit()
    async with tenant_session(tenant_id) as session:
        assert await emit(_event(tenant_id), session) == []


async def test_emit_ignores_an_unverified_destination(wired, admin_session) -> None:
    """An unverified address is one somebody typed. It may not be theirs."""
    tenant_id, _, dest_id = wired
    await admin_session.execute(
        text("UPDATE notification_destinations SET verified_at = NULL WHERE id = :d"),
        {"d": dest_id},
    )
    await admin_session.commit()
    async with tenant_session(tenant_id) as session:
        assert await emit(_event(tenant_id), session) == []


async def test_emit_ignores_a_disabled_destination(wired, admin_session) -> None:
    tenant_id, _, dest_id = wired
    await admin_session.execute(
        text("UPDATE notification_destinations SET disabled_at = now() WHERE id = :d"),
        {"d": dest_id},
    )
    await admin_session.commit()
    async with tenant_session(tenant_id) as session:
        assert await emit(_event(tenant_id), session) == []


async def test_emit_is_idempotent_for_one_opportunity(wired) -> None:
    """The extraction job can be retried. The recruiter must not be told twice."""
    tenant_id, _, _ = wired
    event = _event(tenant_id)
    async with tenant_session(tenant_id) as session:
        first = await emit(event, session)
    async with tenant_session(tenant_id) as session:
        second = await emit(event, session)
    assert len(first) == 1
    assert second == []


async def test_emit_does_not_reach_another_tenant(wired, admin_session) -> None:
    """A destination in tenant A must never receive tenant B's job orders."""
    tenant_id, _, _ = wired
    other = uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'other')"), {"id": other}
    )
    await admin_session.commit()
    try:
        async with tenant_session(other) as session:
            assert await emit(_event(other), session) == []
    finally:
        await admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": other}
        )
        await admin_session.commit()


async def test_rate_cap_suppresses_past_the_hourly_ceiling(wired, admin_session) -> None:
    tenant_id, _, dest_id = wired
    for _ in range(settings.NOTIFY_RATE_CAP_PER_HOUR):
        await admin_session.execute(
            text(
                "INSERT INTO notification_deliveries "
                "(id, tenant_id, destination_id, event_kind, subject_id, status) "
                "VALUES (:id, :tid, :did, :kind, :sub, 'sent')"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "did": dest_id,
                "kind": EVENT_OPPORTUNITY_NEW,
                "sub": uuid.uuid4(),
            },
        )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        assert await rate_capped(session, dest_id, EVENT_OPPORTUNITY_NEW) is True
        ids = await emit(_event(tenant_id), session)

    async with tenant_session(tenant_id) as session:
        status = (
            await session.execute(
                text("SELECT status FROM notification_deliveries WHERE id = :id"),
                {"id": ids[0]},
            )
        ).scalar_one()
    assert status == STATUS_SUPPRESSED


async def test_rate_cap_ignores_sends_older_than_an_hour(wired, admin_session) -> None:
    """The window slides. Yesterday's burst must not mute today."""
    tenant_id, _, dest_id = wired
    stale = datetime.now(UTC) - timedelta(hours=2)
    for _ in range(settings.NOTIFY_RATE_CAP_PER_HOUR + 5):
        await admin_session.execute(
            text(
                "INSERT INTO notification_deliveries "
                "(id, tenant_id, destination_id, event_kind, subject_id, status, created_at) "
                "VALUES (:id, :tid, :did, :kind, :sub, 'sent', :ts)"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "did": dest_id,
                "kind": EVENT_OPPORTUNITY_NEW,
                "sub": uuid.uuid4(),
                "ts": stale,
            },
        )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        assert await rate_capped(session, dest_id, EVENT_OPPORTUNITY_NEW) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notify_dispatch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.notify.dispatch'`

- [ ] **Step 3: Write dispatch**

Create `app/services/notify/dispatch.py`:

```python
"""Event in, outbox rows out.

`emit` takes the caller's session rather than opening its own, so the
notification rows land in the *same* transaction that created the opportunity.
Either both commit or neither does — an opportunity with no notification row is
recoverable, but a notification for an opportunity that rolled back is a
message about something that never happened.

Enqueueing is deliberately separate and happens after that commit. A job that
starts before its row is visible reads nothing and exits, and would then never
be retried.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.notification import STATUS_PENDING, STATUS_SUPPRESSED
from app.services.notify.events import OpportunityEvent
from app.workers.queue import enqueue

log = get_logger(__name__)

# allow-hardcode: SQL statements, not a phrase list.

# A destination only receives if it is verified, not disabled, and actively
# subscribed. All three in one statement so there is no window between the
# check and the insert.
_SUBSCRIBERS = text(
    """
    SELECT d.id AS destination_id, d.channel
    FROM notification_destinations d
    JOIN notification_subscriptions s ON s.destination_id = d.id
    WHERE s.event_kind = :event_kind
      AND s.active
      AND d.verified_at IS NOT NULL
      AND d.disabled_at IS NULL
    """
)

_COUNT_RECENT = text(
    """
    SELECT count(*) FROM notification_deliveries
    WHERE destination_id = :destination_id
      AND event_kind = :event_kind
      AND created_at > now() - interval '1 hour'
      AND status <> 'suppressed'
    """
)

# ON CONFLICT DO NOTHING against the partial dedupe index: the extraction job
# can be retried, and the recruiter must not be told twice about one vacancy.
_INSERT_DELIVERY = text(
    """
    INSERT INTO notification_deliveries
        (id, tenant_id, destination_id, event_kind, subject_id, status)
    VALUES (:id, :tenant_id, :destination_id, :event_kind, :subject_id, :status)
    ON CONFLICT (destination_id, event_kind, subject_id)
      WHERE subject_id IS NOT NULL
      DO NOTHING
    RETURNING id
    """
)


async def rate_capped(
    session: AsyncSession, destination_id: uuid.UUID, event_kind: str
) -> bool:
    """Has this destination already had its hour's worth of this event?

    Counts the rows themselves rather than a stored counter, so the cap cannot
    drift from what was actually sent. Suppressed rows are excluded — counting
    them would make the first suppression permanent.
    """
    recent = (
        await session.execute(
            _COUNT_RECENT,
            {"destination_id": destination_id, "event_kind": event_kind},
        )
    ).scalar_one()
    return recent >= settings.NOTIFY_RATE_CAP_PER_HOUR


async def emit(event: OpportunityEvent, session: AsyncSession) -> list[uuid.UUID]:
    """Write one outbox row per subscriber. Returns the ids worth enqueueing.

    A row over the rate cap is written as `suppressed` rather than skipped: the
    next delivery counts them to say "and N more", and the sweep flushes them
    if no next delivery ever comes.
    """
    subscribers = (
        await session.execute(_SUBSCRIBERS, {"event_kind": event.kind})
    ).all()

    to_enqueue: list[uuid.UUID] = []
    for row in subscribers:
        capped = await rate_capped(session, row.destination_id, event.kind)
        delivery_id = (
            await session.execute(
                _INSERT_DELIVERY,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": event.tenant_id,
                    "destination_id": row.destination_id,
                    "event_kind": event.kind,
                    "subject_id": event.opportunity_id,
                    "status": STATUS_SUPPRESSED if capped else STATUS_PENDING,
                },
            )
        ).scalar_one_or_none()

        # None means the dedupe index rejected it — already told, nothing to do.
        if delivery_id is not None and not capped:
            to_enqueue.append(delivery_id)

    return to_enqueue


async def enqueue_deliveries(
    tenant_id: uuid.UUID, delivery_ids: list[uuid.UUID]
) -> int:
    """Queue the rows the caller has now committed.

    `enqueue` never raises and returns False on failure; the sweep is what
    turns a lost job back into a queued one, exactly as `rescan_stuck` does for
    ingestion.
    """
    queued = 0
    for delivery_id in delivery_ids:
        if await enqueue(
            "deliver_notification",
            delivery_id=str(delivery_id),
            tenant_id=str(tenant_id),
        ):
            queued += 1
    return queued


async def emit_and_enqueue(event: OpportunityEvent) -> int:
    """For callers with no transaction of their own. Opens, commits, enqueues."""
    async with tenant_session(event.tenant_id) as session:
        delivery_ids = await emit(event, session)
    return await enqueue_deliveries(event.tenant_id, delivery_ids)
```

- [ ] **Step 4: Re-export from the package**

Replace `app/services/notify/__init__.py` with:

```python
"""Notification delivery (spec 2026-07-28)."""

from app.services.notify.dispatch import emit, emit_and_enqueue, enqueue_deliveries

__all__ = ["emit", "emit_and_enqueue", "enqueue_deliveries"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_notify_dispatch.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add app/services/notify/ tests/test_notify_dispatch.py
git commit -m "Write the notification in the transaction that made the job order"
```

---

## Task 6: The delivery job

**Files:**
- Modify: `app/workers/jobs.py`
- Modify: `app/workers/settings.py`
- Test: `tests/test_deliver_notification.py`

**Interfaces:**
- Consumes: models (Task 2), `render` (Task 3), `channel_for` / `SendOutcome` (Task 4)
- Produces: `async def deliver_notification(ctx, *, delivery_id: str, tenant_id: str) -> None` in `app/workers/jobs.py`, registered in `WorkerSettings.functions`

- [ ] **Step 1: Write the failing test**

Create `tests/test_deliver_notification.py`:

```python
"""The delivery job: claim once, send once, and make a dead address visible."""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_TELEGRAM,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    address_digest,
)
from app.services.notify.channels.base import SendOutcome, SendResult
from app.workers import jobs


class FakeChannel:
    """Records what it was asked to send and answers however the test says."""

    def __init__(self, result: SendResult) -> None:
        self.result = result
        self.sends: list[tuple[str, object]] = []

    async def send(self, address: str, content) -> SendResult:
        self.sends.append((address, content))
        return self.result


@pytest.fixture
async def delivery(admin_session):
    """A pending delivery to a verified Telegram destination."""
    from app.core.crypto import encrypt
    from app.services.notify.events import EVENT_OPPORTUNITY_NEW

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    dest_id, delivery_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'agency')"), {"id": tenant_id}
    )
    await admin_session.execute(
        text("INSERT INTO users (id, tenant_id, email) VALUES (:id, :tid, 'r@a.sg')"),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, :enc, :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "enc": encrypt("12345"),
            "hash": address_digest("12345"),
        },
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_deliveries "
            "(id, tenant_id, destination_id, event_kind, subject_id, status) "
            "VALUES (:id, :tid, :did, :kind, :sub, 'pending')"
        ),
        {
            "id": delivery_id,
            "tid": tenant_id,
            "did": dest_id,
            "kind": EVENT_OPPORTUNITY_NEW,
            "sub": uuid.uuid4(),
        },
    )
    await admin_session.commit()
    yield tenant_id, dest_id, delivery_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def _status(tenant_id, delivery_id) -> str:
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text("SELECT status FROM notification_deliveries WHERE id = :id"),
                {"id": delivery_id},
            )
        ).scalar_one()


async def test_successful_send_marks_the_row_sent(delivery, monkeypatch) -> None:
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="42"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )

    assert await _status(tenant_id, delivery_id) == STATUS_SENT
    assert len(fake.sends) == 1


async def test_the_channel_receives_the_decrypted_address(delivery, monkeypatch) -> None:
    """The column holds ciphertext; Telegram needs the chat id."""
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="1"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )
    assert fake.sends[0][0] == "12345"


async def test_a_second_run_does_not_send_again(delivery, monkeypatch) -> None:
    """The sweep and the original enqueue can both fire for one row."""
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="1"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    for _ in range(2):
        await jobs.deliver_notification(
            {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )
    assert len(fake.sends) == 1


async def test_permanent_failure_disables_the_destination(delivery, monkeypatch) -> None:
    """A dead address must become visible, not absorb messages forever."""
    tenant_id, dest_id, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.PERMANENT, error="bot blocked"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )

    assert await _status(tenant_id, delivery_id) == STATUS_FAILED
    async with tenant_session(tenant_id) as session:
        disabled = (
            await session.execute(
                text("SELECT disabled_at FROM notification_destinations WHERE id = :id"),
                {"id": dest_id},
            )
        ).scalar_one()
    assert disabled is not None


async def test_transient_failure_returns_the_row_to_pending(delivery, monkeypatch) -> None:
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.TRANSIENT, error="503"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    with pytest.raises(Exception):
        # Raising is how arq is told to retry — see the job's docstring.
        await jobs.deliver_notification(
            {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )

    assert await _status(tenant_id, delivery_id) == STATUS_PENDING


async def test_transient_failure_past_max_attempts_gives_up(
    delivery, monkeypatch, admin_session
) -> None:
    """A message about a vacancy from six hours ago is not worth a seventh try."""
    tenant_id, _, delivery_id = delivery
    await admin_session.execute(
        text("UPDATE notification_deliveries SET attempts = :n WHERE id = :id"),
        {"n": settings.NOTIFY_MAX_ATTEMPTS, "id": delivery_id},
    )
    await admin_session.commit()

    fake = FakeChannel(SendResult(outcome=SendOutcome.TRANSIENT, error="503"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )
    assert await _status(tenant_id, delivery_id) == STATUS_FAILED


async def test_a_missing_row_is_not_an_error(monkeypatch) -> None:
    """RLS already decided the job's tenant does not own this row."""
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)
    await jobs.deliver_notification(
        {}, delivery_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4())
    )
    assert fake.sends == []


def test_the_job_is_registered_with_arq() -> None:
    """A name a producer enqueues but the registry omits fails inside arq, past
    the queue, where the producer already saw success."""
    from app.workers.settings import WorkerSettings

    assert jobs.deliver_notification in WorkerSettings.functions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deliver_notification.py -v`
Expected: FAIL with `AttributeError: module 'app.workers.jobs' has no attribute 'deliver_notification'`

- [ ] **Step 3: Write the job**

Add to the imports at the top of `app/workers/jobs.py`:

```python
from app.core.crypto import decrypt
from app.models.notification import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENDING,
    STATUS_SENT,
    STATUS_SUPPRESSED,
)
from app.services.notify.channels import channel_for
from app.services.notify.channels.base import SendOutcome
from app.services.notify.events import OpportunityEvent
from app.services.notify.render import render
```

Add these statements beside the other `text()` constants in the module:

```python
# allow-hardcode: SQL statements, not a phrase list.

# The claim. Claiming *after* the send would double-message when the sweep and
# the original enqueue both fire for one row, which they can and do.
_CLAIM_DELIVERY = text(
    """
    UPDATE notification_deliveries
    SET status = :sending, attempts = attempts + 1
    WHERE id = :id AND status = :pending
    RETURNING id, destination_id, event_kind, subject_id, attempts
    """
)

_DELIVERY_TARGET = text(
    """
    SELECT d.channel, d.address_encrypted, d.failure_count
    FROM notification_destinations d
    WHERE d.id = :destination_id AND d.disabled_at IS NULL
    """
)

# The event is re-read at send time rather than carried through Redis: a job
# payload is not a place to put a job title, and the row is one join away.
_DELIVERY_SUBJECT = text(
    """
    SELECT job_title_raw, company_name_raw, location_raw, salary_raw
    FROM opportunities WHERE id = :opportunity_id
    """
)

# Suppressed rows since this destination's last completed delivery. This is the
# "+N more" the next message carries, and marking them accounted-for here is
# what stops the same batch being reported twice.
_CLAIM_ROLLUP = text(
    """
    UPDATE notification_deliveries
    SET status = :failed, error = 'rolled up'
    WHERE destination_id = :destination_id
      AND event_kind = :event_kind
      AND status = :suppressed
    RETURNING id
    """
)

_FINISH_DELIVERY = text(
    """
    UPDATE notification_deliveries
    SET status = :status, provider_message_id = :provider_message_id,
        error = :error, sent_at = CASE WHEN :status = 'sent' THEN now() ELSE NULL END
    WHERE id = :id
    """
)

_RECORD_FAILURE = text(
    """
    UPDATE notification_destinations
    SET failure_count = failure_count + 1,
        disabled_at = CASE
            WHEN failure_count + 1 >= :max_failures THEN now() ELSE disabled_at
        END
    WHERE id = :id
    """
)

_RESET_FAILURES = text(
    "UPDATE notification_destinations SET failure_count = 0 WHERE id = :id"
)
```

Add the job function at the end of `app/workers/jobs.py`:

```python
async def deliver_notification(ctx, *, delivery_id: str, tenant_id: str) -> None:
    """Send one outbox row.

    Claims before sending. The sweep and the original enqueue can both fire for
    the same row, and claiming afterwards would double-message; `RETURNING`
    with a `status = 'pending'` predicate makes the claim atomic, so the loser
    of the race gets no row and exits.

    A transient failure *raises*, because arq's retry is driven by exceptions.
    A permanent one does not — it disables the destination and returns, since
    retrying an address that will never accept a message is throughput spent on
    nothing.
    """
    tenant = uuid.UUID(tenant_id)

    async with tenant_session(tenant) as session:
        claimed = (
            await session.execute(
                _CLAIM_DELIVERY,
                {"id": delivery_id, "sending": STATUS_SENDING, "pending": STATUS_PENDING},
            )
        ).one_or_none()

    if claimed is None:
        # Already claimed, already sent, or owned by another tenant. RLS
        # already decided; there is nothing to do and nothing to report.
        log.info("delivery_skipped", delivery_id=delivery_id)
        return

    async with tenant_session(tenant) as session:
        target = (
            await session.execute(
                _DELIVERY_TARGET, {"destination_id": claimed.destination_id}
            )
        ).one_or_none()

        if target is None:
            # Disabled between emit and delivery. Not a failure of this row.
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_FAILED,
                    "provider_message_id": None,
                    "error": "destination disabled",
                },
            )
            return

        subject = (
            await session.execute(
                _DELIVERY_SUBJECT, {"opportunity_id": claimed.subject_id}
            )
        ).one_or_none()

        rolled = (
            await session.execute(
                _CLAIM_ROLLUP,
                {
                    "destination_id": claimed.destination_id,
                    "event_kind": claimed.event_kind,
                    "suppressed": STATUS_SUPPRESSED,
                    "failed": STATUS_FAILED,
                },
            )
        ).all()

    if subject is None:
        # The opportunity was deleted after emit. Nothing to say about it.
        async with tenant_session(tenant) as session:
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_FAILED,
                    "provider_message_id": None,
                    "error": "subject no longer exists",
                },
            )
        return

    event = OpportunityEvent(
        kind=claimed.event_kind,
        tenant_id=tenant,
        opportunity_id=claimed.subject_id,
        job_title=subject.job_title_raw,
        company_name=subject.company_name_raw,
        location=subject.location_raw,
        salary=subject.salary_raw,
    )
    content = render(event, target.channel, rollup=len(rolled))

    result = await channel_for(target.channel).send(
        decrypt(target.address_encrypted), content
    )

    if result.outcome is SendOutcome.SENT:
        async with tenant_session(tenant) as session:
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_SENT,
                    "provider_message_id": result.provider_message_id,
                    "error": None,
                },
            )
            # A success clears the count, so three failures spread over a month
            # do not add up to a disabled destination.
            await session.execute(_RESET_FAILURES, {"id": claimed.destination_id})
        return

    if result.outcome is SendOutcome.PERMANENT:
        async with tenant_session(tenant) as session:
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_FAILED,
                    "provider_message_id": None,
                    "error": result.error,
                },
            )
            await session.execute(
                _RECORD_FAILURE,
                {"id": claimed.destination_id, "max_failures": 1},
            )
        log.warning(
            "delivery_permanently_failed",
            delivery_id=delivery_id,
            channel=target.channel,
            error=result.error,
        )
        return

    # Transient.
    if claimed.attempts >= settings.NOTIFY_MAX_ATTEMPTS:
        async with tenant_session(tenant) as session:
            await session.execute(
                _FINISH_DELIVERY,
                {
                    "id": delivery_id,
                    "status": STATUS_FAILED,
                    "provider_message_id": None,
                    "error": f"gave up after {claimed.attempts} attempts: {result.error}",
                },
            )
            await session.execute(
                _RECORD_FAILURE,
                {
                    "id": claimed.destination_id,
                    "max_failures": settings.NOTIFY_MAX_FAILURES,
                },
            )
        return

    async with tenant_session(tenant) as session:
        await session.execute(
            _FINISH_DELIVERY,
            {
                "id": delivery_id,
                "status": STATUS_PENDING,
                "provider_message_id": None,
                "error": result.error,
            },
        )
    # arq retries on an exception and on nothing else. Releasing the claim
    # first means the retry — or the sweep, whichever arrives — finds a row it
    # can claim rather than one stuck in `sending`.
    raise RuntimeError(f"Transient notification failure: {result.error}")
```

> **Note on `_RECORD_FAILURE` with `max_failures: 1`:** a permanent failure disables on the spot, so the threshold is passed as 1 rather than the configured value. The configured `NOTIFY_MAX_FAILURES` governs the transient-exhaustion path, where several independent failures must accumulate before an address is judged dead.

- [ ] **Step 4: Register the job**

In `app/workers/settings.py`, add `deliver_notification` to the import from `app.workers.jobs` and to `WorkerSettings.functions`, with this comment above the entry:

```python
        # Notifications. Enqueued by `emit_and_enqueue` after the opportunity
        # commits, and by `flush_notifications` for rows whose enqueue was lost.
        deliver_notification,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_deliver_notification.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add app/workers/jobs.py app/workers/settings.py tests/test_deliver_notification.py
git commit -m "Claim the row before sending, not after"
```

---

## Task 7: The recovery sweep

**Files:**
- Modify: `app/workers/tasks.py`
- Modify: `app/workers/main.py`
- Test: `tests/test_notify_sweep.py`

**Interfaces:**
- Consumes: models (Task 2), `enqueue` from `app.workers.queue`
- Produces: `async def flush_notifications() -> int` in `app/workers/tasks.py`, registered as a `PeriodicTask` named `flush_notifications` in `build_tasks()`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_sweep.py`:

```python
"""The sweep has two duties: lost enqueues, and the rollup's tail."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models.notification import CHANNEL_TELEGRAM, address_digest
from app.services.notify.events import EVENT_OPPORTUNITY_NEW
from app.workers import tasks


@pytest.fixture
async def scene(admin_session):
    tenant_id, user_id, dest_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'agency')"), {"id": tenant_id}
    )
    await admin_session.execute(
        text("INSERT INTO users (id, tenant_id, email) VALUES (:id, :tid, 'r@a.sg')"),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'ciphertext', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.commit()
    yield tenant_id, dest_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def _insert(admin_session, tenant_id, dest_id, status, age_minutes) -> uuid.UUID:
    row_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_deliveries "
            "(id, tenant_id, destination_id, event_kind, subject_id, status, created_at) "
            "VALUES (:id, :tid, :did, :kind, :sub, :status, :ts)"
        ),
        {
            "id": row_id,
            "tid": tenant_id,
            "did": dest_id,
            "kind": EVENT_OPPORTUNITY_NEW,
            "sub": uuid.uuid4(),
            "status": status,
            "ts": datetime.now(UTC) - timedelta(minutes=age_minutes),
        },
    )
    await admin_session.commit()
    return row_id


async def test_a_stale_pending_row_is_requeued(scene, admin_session, monkeypatch) -> None:
    """This is the lost-enqueue net. Without it, 'no notification is lost' is
    simply false — `enqueue` fails soft by design."""
    tenant_id, dest_id = scene
    row_id = await _insert(
        admin_session,
        tenant_id,
        dest_id,
        "pending",
        settings.NOTIFY_DELIVERY_STALE_MINUTES + 5,
    )
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append({"name": name, **kwargs})
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()

    assert any(q["delivery_id"] == str(row_id) for q in queued)
    assert queued[0]["name"] == "deliver_notification"
    assert queued[0]["tenant_id"] == str(tenant_id)


async def test_a_fresh_pending_row_is_left_alone(scene, admin_session, monkeypatch) -> None:
    """Otherwise the sweep competes with a job that is merely slow."""
    tenant_id, dest_id = scene
    await _insert(admin_session, tenant_id, dest_id, "pending", 1)
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()
    assert queued == []


async def test_an_orphaned_suppressed_batch_is_flushed(
    scene, admin_session, monkeypatch
) -> None:
    """The rollup's tail. If no further event ever arrives, '+N more' is lost
    forever without this."""
    tenant_id, dest_id = scene
    await _insert(admin_session, tenant_id, dest_id, "suppressed", 90)
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()
    assert len(queued) == 1


async def test_a_suppressed_row_inside_the_cap_window_is_left_alone(
    scene, admin_session, monkeypatch
) -> None:
    """Flushing it early would defeat the cap it was suppressed by."""
    tenant_id, dest_id = scene
    await _insert(admin_session, tenant_id, dest_id, "suppressed", 5)
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()
    assert queued == []


def test_the_sweep_is_registered_in_the_supervisor() -> None:
    from app.workers.main import build_tasks

    assert "flush_notifications" in {t.name for t in build_tasks()}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notify_sweep.py -v`
Expected: FAIL with `AttributeError: module 'app.workers.tasks' has no attribute 'flush_notifications'`

- [ ] **Step 3: Write the sweep**

Add to `app/workers/tasks.py`:

```python
# allow-hardcode: SQL statement, not a phrase list.
#
# Two kinds of row, one statement, because they need the same treatment: an
# enqueue that must happen and did not.
#
# `pending` past the stale window means the enqueue was lost — `enqueue` fails
# soft after the transaction committed, so the row is durable and the job is
# not. `suppressed` past the cap window means the rate cap ate a message and no
# later delivery arrived to carry its "+N more", so the batch would otherwise
# go unmentioned forever.
#
# The suppressed row is promoted to `pending` in the same statement that
# selects it, so the next tick cannot claim the same batch twice.
_FLUSHABLE_DELIVERIES = text(
    """
    UPDATE notification_deliveries
    SET status = 'pending'
    WHERE id IN (
        SELECT id FROM notification_deliveries
        WHERE (status = 'pending' AND created_at < now() - make_interval(mins => :stale_minutes))
           OR (status = 'suppressed' AND created_at < now() - interval '1 hour')
        ORDER BY created_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, tenant_id
    """
)


async def flush_notifications() -> int:
    """Queue notifications nothing else is going to send.

    Runs unscoped, across every tenant at once, like the other sweeps here —
    hence the raw statement rather than a tenant session. Each row carries its
    own tenant, and the job re-reads it under that tenant's policy.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                _FLUSHABLE_DELIVERIES,
                {
                    "stale_minutes": settings.NOTIFY_DELIVERY_STALE_MINUTES,
                    "limit": settings.NOTIFY_FLUSH_LIMIT,
                },
            )
        ).all()
        # The promotion is an UPDATE and only takes effect on commit. Without
        # this the rows stay suppressed and the next tick claims them again.
        await session.commit()

    queued = 0
    for row in rows:
        if await enqueue(
            "deliver_notification",
            delivery_id=str(row.id),
            tenant_id=str(row.tenant_id),
        ):
            queued += 1

    if queued:
        # Worth noticing rather than silently absorbing: every row here is one
        # the normal path should have carried and did not.
        log.warning("notifications_flushed", count=queued)
    return queued
```

This needs one more setting. Add to `app/core/config.py` beside the other `NOTIFY_` keys, and to `.env.example` as `NOTIFY_FLUSH_LIMIT=200`:

```python
    # Bounds one tick's work, so a backlog drains steadily instead of queueing
    # ten thousand jobs in a single sweep.
    NOTIFY_FLUSH_LIMIT: int = Field(default=200, gt=0)
```

- [ ] **Step 4: Register the sweep**

In `app/workers/main.py`, add `flush_notifications` to the import inside `build_tasks()`, then add the wrapper and the registry entry:

```python
    async def _flush_notifications() -> None:
        # Both duties on one clock: a delivery whose enqueue was lost, and a
        # rate-capped batch with no later message to carry its "+N more".
        await flush_notifications()
```

```python
        PeriodicTask(
            "flush_notifications",
            settings.NOTIFY_SWEEP_INTERVAL_SECONDS,
            _flush_notifications,
        ),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_notify_sweep.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add app/workers/tasks.py app/workers/main.py app/core/config.py .env.example tests/test_notify_sweep.py
git commit -m "Stop the last capped batch from going unmentioned"
```

---

## Task 8: Linking

**Files:**
- Create: `app/services/notify/linking.py`
- Test: `tests/test_notify_linking.py`

**Interfaces:**
- Consumes: models (Task 2), `settings` (Task 1), `app.core.crypto`
- Produces:
  - `async def issue_token(session, tenant_id, user_id, channel, address: str | None = None) -> str` — returns the plaintext token, storing only its hash
  - `async def redeem_token(session, token: str, channel: str) -> LinkedToken | None`
  - `@dataclass(frozen=True) class LinkedToken: tenant_id: uuid.UUID; user_id: uuid.UUID; address: str | None`
  - `async def create_destination(session, tenant_id, user_id, channel, address: str) -> uuid.UUID`
  - `def generate_code() -> str` — the six-digit WhatsApp opt-in code
  - `async def opt_in_attempts_this_hour(session, user_id: uuid.UUID, channel: str) -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_linking.py`:

```python
"""Linking proves the address belongs to whoever asked for it."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP
from app.services.notify.linking import (
    create_destination,
    generate_code,
    issue_token,
    redeem_token,
)


@pytest.fixture
async def account(admin_session):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'agency')"), {"id": tenant_id}
    )
    await admin_session.execute(
        text("INSERT INTO users (id, tenant_id, email) VALUES (:id, :tid, 'r@a.sg')"),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.commit()
    yield tenant_id, user_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def test_the_plaintext_token_is_never_stored(account) -> None:
    """A token in the clear leaks from a backup into someone else's job orders."""
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    async with tenant_session(tenant_id) as session:
        stored = (
            await session.execute(
                text("SELECT token_hash FROM notification_link_tokens")
            )
        ).scalars().all()
    assert token not in stored


async def test_a_token_redeems_once(account) -> None:
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    async with tenant_session(tenant_id) as session:
        first = await redeem_token(session, token, CHANNEL_TELEGRAM)
    assert first is not None
    assert first.user_id == user_id

    async with tenant_session(tenant_id) as session:
        assert await redeem_token(session, token, CHANNEL_TELEGRAM) is None


async def test_an_expired_token_is_refused(account, admin_session) -> None:
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    await admin_session.execute(
        text("UPDATE notification_link_tokens SET expires_at = :past"),
        {"past": datetime.now(UTC) - timedelta(minutes=1)},
    )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        assert await redeem_token(session, token, CHANNEL_TELEGRAM) is None


async def test_a_token_for_one_channel_does_not_redeem_on_another(account) -> None:
    """A Telegram start-token must not become a verified phone number."""
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)
    async with tenant_session(tenant_id) as session:
        assert await redeem_token(session, token, CHANNEL_WHATSAPP) is None


async def test_a_whatsapp_token_carries_the_number_it_was_sent_to(account) -> None:
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(
            session, tenant_id, user_id, CHANNEL_WHATSAPP, address="+6591234567"
        )
    async with tenant_session(tenant_id) as session:
        redeemed = await redeem_token(session, token, CHANNEL_WHATSAPP)
    assert redeemed.address == "+6591234567"


async def test_a_created_destination_is_verified_and_encrypted(account) -> None:
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        dest_id = await create_destination(
            session, tenant_id, user_id, CHANNEL_TELEGRAM, "12345"
        )

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT address_encrypted, verified_at FROM "
                    "notification_destinations WHERE id = :id"
                ),
                {"id": dest_id},
            )
        ).one()
    assert row.address_encrypted != "12345"
    assert row.verified_at is not None


async def test_relinking_the_same_address_reuses_the_destination(account) -> None:
    """Otherwise the unique constraint turns 'link it again' into a 500."""
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        first = await create_destination(
            session, tenant_id, user_id, CHANNEL_TELEGRAM, "12345"
        )
    async with tenant_session(tenant_id) as session:
        second = await create_destination(
            session, tenant_id, user_id, CHANNEL_TELEGRAM, "12345"
        )
    assert first == second


def test_the_code_is_six_digits() -> None:
    code = generate_code()
    assert len(code) == 6
    assert code.isdigit()


def test_codes_differ() -> None:
    assert len({generate_code() for _ in range(50)}) > 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notify_linking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.notify.linking'`

- [ ] **Step 3: Write linking**

Create `app/services/notify/linking.py`:

```python
"""Proving an address belongs to the person who asked for it.

A typed identifier that is one digit wrong delivers a client's job orders to a
stranger, so nothing is ever verified by assertion. Telegram proves it by the
fact that only the account holder could have pressed start on a link; WhatsApp
by a code that only reaches the number typed.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt, encrypt
from app.models.notification import address_digest

# The token is compared by hash, so it must hash the same way every time —
# `address_digest` is the same construction and the same reasoning.
_hash_token = address_digest

# allow-hardcode: SQL statements, not a phrase list.
_INSERT_TOKEN = text(
    """
    INSERT INTO notification_link_tokens
        (id, tenant_id, user_id, channel, token_hash, address_encrypted, expires_at)
    VALUES (:id, :tenant_id, :user_id, :channel, :token_hash, :address_encrypted, :expires_at)
    """
)

# Consumes in the statement that reads, so a token raced by two requests
# resolves for exactly one of them.
_CONSUME_TOKEN = text(
    """
    UPDATE notification_link_tokens
    SET consumed_at = now()
    WHERE token_hash = :token_hash
      AND channel = :channel
      AND consumed_at IS NULL
      AND expires_at > now()
    RETURNING tenant_id, user_id, address_encrypted
    """
)

_UPSERT_DESTINATION = text(
    """
    INSERT INTO notification_destinations
        (id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at)
    VALUES (:id, :tenant_id, :user_id, :channel, :address_encrypted, :address_hash, now())
    ON CONFLICT (tenant_id, channel, address_hash) DO UPDATE
      SET verified_at = now(), disabled_at = NULL, failure_count = 0
    RETURNING id
    """
)

_OPT_IN_ATTEMPTS = text(
    """
    SELECT count(*) FROM notification_link_tokens
    WHERE user_id = :user_id
      AND channel = :channel
      AND created_at > now() - interval '1 hour'
    """
)


@dataclass(frozen=True)
class LinkedToken:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    address: str | None


def generate_code() -> str:
    """A six-digit opt-in code.

    `secrets`, not `random`: this is the only thing standing between a guess
    and a stranger's job orders.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


async def issue_token(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    channel: str,
    address: str | None = None,
) -> str:
    """Mint a single-use token. Returns the plaintext; stores only its hash.

    The plaintext is returned once and never again. Losing it means issuing
    another, which is cheap — recovering it from the database is meant to be
    impossible.
    """
    token = generate_code() if address is not None else secrets.token_urlsafe(24)
    await session.execute(
        _INSERT_TOKEN,
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "user_id": user_id,
            "channel": channel,
            "token_hash": _hash_token(token),
            "address_encrypted": encrypt(address) if address else None,
            "expires_at": datetime.now(UTC)
            + timedelta(minutes=settings.NOTIFY_LINK_TOKEN_TTL_MINUTES),
        },
    )
    return token


async def redeem_token(
    session: AsyncSession, token: str, channel: str
) -> LinkedToken | None:
    """Consume a token, or return None if it is spent, expired, or not ours.

    The channel is part of the predicate on purpose: without it a Telegram
    start-token would redeem as a verified phone number.
    """
    row = (
        await session.execute(
            _CONSUME_TOKEN,
            {"token_hash": _hash_token(token), "channel": channel},
        )
    ).one_or_none()
    if row is None:
        return None
    return LinkedToken(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        address=decrypt(row.address_encrypted) if row.address_encrypted else None,
    )


async def create_destination(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    channel: str,
    address: str,
) -> uuid.UUID:
    """Record a verified destination, or revive the one already there.

    Upsert rather than insert: re-linking an address someone already had is an
    ordinary thing to do, and a bare insert would turn it into a constraint
    violation with nothing to explain it. Reviving also clears `disabled_at`,
    which is how a recruiter who blocked the bot and changed their mind gets
    notifications back.
    """
    return (
        await session.execute(
            _UPSERT_DESTINATION,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "channel": channel,
                "address_encrypted": encrypt(address),
                "address_hash": address_digest(address),
            },
        )
    ).scalar_one()


async def opt_in_attempts_this_hour(
    session: AsyncSession, user_id: uuid.UUID, channel: str
) -> int:
    """How many codes this user has asked us to send.

    Sending an authentication template to any number a user types is an OTP
    pump aimed at our WABA's reputation, and the reputation is shared by every
    tenant on the number.
    """
    return (
        await session.execute(
            _OPT_IN_ATTEMPTS, {"user_id": user_id, "channel": channel}
        )
    ).scalar_one()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_notify_linking.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/notify/linking.py tests/test_notify_linking.py
git commit -m "Make a mistyped number fail to link rather than link to a stranger"
```

---

## Task 9: Preferences and linking API

**Files:**
- Create: `app/api/notifications.py`
- Modify: `app/main.py`
- Test: `tests/test_notifications_api.py`

**Interfaces:**
- Consumes: `_require_session` from `app.api.auth`, linking (Task 8), events (Task 3), channels (Task 4)
- Produces: `router` with `GET /notifications/settings`, `PUT /notifications/subscriptions`, `POST /notifications/destinations/telegram/link`, `POST /notifications/destinations/whatsapp/opt-in`, `POST /notifications/destinations/whatsapp/verify`, `PUT /notifications/destinations/{destination_id}/scope`, `DELETE /notifications/destinations/{destination_id}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notifications_api.py`:

```python
"""The settings surface. Authentication and tenant scope are the point."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import settings
from app.main import app
from app.models.notification import CHANNEL_TELEGRAM, address_digest
from app.services.notify.events import ALL_EVENT_KINDS, EVENT_OPPORTUNITY_NEW


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
async def signed_in(admin_session, client):
    """A tenant, a user, and the session cookie that authenticates them."""
    from app.api.auth import SESSION_COOKIE, _session_serializer

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'agency')"), {"id": tenant_id}
    )
    await admin_session.execute(
        text("INSERT INTO users (id, tenant_id, email) VALUES (:id, :tid, 'r@a.sg')"),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.commit()
    cookie = _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)})
    client.cookies.set(SESSION_COOKIE, cookie)
    yield tenant_id, user_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def test_settings_requires_a_session(client) -> None:
    client.cookies.clear()
    assert client.get("/api/notifications/settings").status_code == 401


async def test_settings_lists_every_event_kind(client, signed_in) -> None:
    """The screen cannot offer an event the backend does not know about."""
    body = client.get("/api/notifications/settings").json()
    assert {e["kind"] for e in body["events"]} == set(ALL_EVENT_KINDS)


async def test_settings_starts_with_no_destinations(client, signed_in) -> None:
    assert client.get("/api/notifications/settings").json()["destinations"] == []


async def test_telegram_link_returns_a_deep_link(client, signed_in) -> None:
    response = client.post("/api/notifications/destinations/telegram/link")
    assert response.status_code == 200
    assert response.json()["url"].startswith("https://t.me/")


async def test_subscriptions_reject_an_unknown_event(
    client, signed_in, admin_session
) -> None:
    """A typo would otherwise be stored as a category nobody is subscribed to."""
    tenant_id, user_id = signed_in
    dest_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'x', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.commit()

    response = client.put(
        "/api/notifications/subscriptions",
        json={"destination_id": str(dest_id), "event_kinds": ["opportunity.invented"]},
    )
    assert response.status_code == 422


async def test_subscriptions_cannot_target_another_tenants_destination(
    client, signed_in, admin_session
) -> None:
    """The most important test in this file."""
    other_tenant, other_dest = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'other')"),
        {"id": other_tenant},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :ch, 'x', :hash, now())"
        ),
        {
            "id": other_dest,
            "tid": other_tenant,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("99999"),
        },
    )
    await admin_session.commit()
    try:
        response = client.put(
            "/api/notifications/subscriptions",
            json={
                "destination_id": str(other_dest),
                "event_kinds": [EVENT_OPPORTUNITY_NEW],
            },
        )
        assert response.status_code == 404
    finally:
        await admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": other_tenant}
        )
        await admin_session.commit()


async def test_opt_in_is_rate_limited(client, signed_in, monkeypatch) -> None:
    """Otherwise this endpoint is an OTP pump on our WABA's reputation."""
    sent: list[str] = []

    class FakeChannel:
        async def send(self, address, content):
            from app.services.notify.channels.base import SendOutcome, SendResult

            sent.append(address)
            return SendResult(outcome=SendOutcome.SENT, provider_message_id="1")

    import app.api.notifications as api_notifications

    monkeypatch.setattr(api_notifications, "channel_for", lambda name: FakeChannel())

    last = None
    for _ in range(settings.NOTIFY_OPT_IN_MAX_PER_HOUR + 1):
        last = client.post(
            "/api/notifications/destinations/whatsapp/opt-in",
            json={"phone_number": "+6591234567"},
        )
    assert last.status_code == 429
    assert len(sent) == settings.NOTIFY_OPT_IN_MAX_PER_HOUR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notifications_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.notifications'`

- [ ] **Step 3: Write the API**

Create `app/api/notifications.py`:

```python
"""Choosing what gets sent where (spec 2026-07-28).

Every endpoint reads the tenant from the session cookie and works inside
`tenant_session`, so a destination id belonging to another agency simply is not
found — the policy answers before any code here has to.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.api.auth import _require_session
from app.db.rls import tenant_session
from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP
from app.services.notify.channels import channel_for
from app.services.notify.channels.base import SendOutcome
from app.services.notify.events import ALL_EVENT_KINDS
from app.services.notify.linking import (
    create_destination,
    issue_token,
    opt_in_attempts_this_hour,
    redeem_token,
)
from app.services.notify.render import WhatsAppContent

log = get_logger(__name__)

router = APIRouter(tags=["notifications"])

# allow-hardcode: SQL statements, not a phrase list.
_LIST_DESTINATIONS = text(
    """
    SELECT d.id, d.channel, d.user_id, d.verified_at, d.disabled_at,
           coalesce(
               array_agg(s.event_kind) FILTER (WHERE s.active), ARRAY[]::text[]
           ) AS event_kinds
    FROM notification_destinations d
    LEFT JOIN notification_subscriptions s ON s.destination_id = d.id
    GROUP BY d.id
    ORDER BY d.created_at
    """
)

_DESTINATION_EXISTS = text(
    "SELECT id FROM notification_destinations WHERE id = :id"
)

_CLEAR_SUBSCRIPTIONS = text(
    "DELETE FROM notification_subscriptions WHERE destination_id = :destination_id"
)

_ADD_SUBSCRIPTION = text(
    """
    INSERT INTO notification_subscriptions
        (id, tenant_id, destination_id, event_kind, active)
    VALUES (:id, :tenant_id, :destination_id, :event_kind, true)
    """
)

_DELETE_DESTINATION = text(
    "DELETE FROM notification_destinations WHERE id = :id RETURNING id"
)


class SubscriptionUpdate(BaseModel):
    destination_id: uuid.UUID
    event_kinds: list[str] = Field(default_factory=list)

    @field_validator("event_kinds")
    @classmethod
    def known_events_only(cls, value: list[str]) -> list[str]:
        """422 rather than a stored typo.

        An unknown kind would be accepted, displayed, and never fired — a
        subscription that looks active and is not.
        """
        unknown = sorted(set(value) - set(ALL_EVENT_KINDS))
        if unknown:
            raise ValueError(f"Unknown event kinds: {', '.join(unknown)}")
        return value


class OptInRequest(BaseModel):
    # E.164. Meta rejects anything else, and the rejection arrives as a failed
    # send with no obvious cause.
    phone_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")


class VerifyRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


@router.get("/notifications/settings")
async def notification_settings(request: Request) -> dict:
    """Everything the settings screen needs in one read."""
    _user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        rows = (await session.execute(_LIST_DESTINATIONS)).all()

    return {
        "events": [{"kind": kind} for kind in ALL_EVENT_KINDS],
        "channels": {
            CHANNEL_TELEGRAM: settings.telegram_configured(),
            CHANNEL_WHATSAPP: settings.whatsapp_configured(),
        },
        "destinations": [
            {
                "id": str(row.id),
                "channel": row.channel,
                # Null means the agency's shared feed rather than one person's.
                "scope": "tenant" if row.user_id is None else "user",
                "verified": row.verified_at is not None,
                "disabled": row.disabled_at is not None,
                "event_kinds": list(row.event_kinds),
            }
            for row in rows
        ],
    }


@router.put("/notifications/subscriptions")
async def set_subscriptions(request: Request, payload: SubscriptionUpdate) -> dict:
    """Replace this destination's subscriptions with exactly what was sent.

    Replace rather than merge: the screen sends the full set of ticked boxes,
    and a merge would make unticking one impossible.
    """
    _user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        exists = (
            await session.execute(
                _DESTINATION_EXISTS, {"id": payload.destination_id}
            )
        ).one_or_none()
        if exists is None:
            # Under RLS another tenant's destination reads as absent, which is
            # the honest answer: it does not exist for this caller.
            raise HTTPException(status_code=404, detail="Destination not found.")

        await session.execute(
            _CLEAR_SUBSCRIPTIONS, {"destination_id": payload.destination_id}
        )
        for kind in payload.event_kinds:
            await session.execute(
                _ADD_SUBSCRIPTION,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "destination_id": payload.destination_id,
                    "event_kind": kind,
                },
            )

    return {"status": "updated", "event_kinds": payload.event_kinds}


@router.post("/notifications/destinations/telegram/link")
async def telegram_link(request: Request) -> dict:
    """A one-time deep link. Pressing it is what proves the account is theirs."""
    user_id, tenant_id = _require_session(request)
    if not settings.telegram_configured():
        raise HTTPException(status_code=503, detail="Telegram is not configured.")

    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    return {
        "url": f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={token}",
        "expires_in_minutes": settings.NOTIFY_LINK_TOKEN_TTL_MINUTES,
    }


@router.post("/notifications/destinations/whatsapp/opt-in")
async def whatsapp_opt_in(request: Request, payload: OptInRequest) -> dict:
    """Send a code to a number the user typed.

    Rate limited because it is otherwise an OTP pump: anyone signed in could
    have us message arbitrary numbers, and the reputation being spent belongs
    to a WABA every tenant shares.
    """
    user_id, tenant_id = _require_session(request)
    if not settings.whatsapp_configured():
        raise HTTPException(status_code=503, detail="WhatsApp is not configured.")

    async with tenant_session(tenant_id) as session:
        attempts = await opt_in_attempts_this_hour(session, user_id, CHANNEL_WHATSAPP)
        if attempts >= settings.NOTIFY_OPT_IN_MAX_PER_HOUR:
            raise HTTPException(
                status_code=429,
                detail="Too many verification codes requested. Try again in an hour.",
            )
        code = await issue_token(
            session, tenant_id, user_id, CHANNEL_WHATSAPP, address=payload.phone_number
        )

    result = await channel_for(CHANNEL_WHATSAPP).send(
        payload.phone_number,
        WhatsAppContent(
            template_name=settings.WHATSAPP_TEMPLATE_LINK_CODE,
            language=settings.WHATSAPP_TEMPLATE_LANG,
            body_params=[code],
            # Meta's authentication templates carry the code on the button too,
            # which is what makes the one-tap copy work in the app.
            button_param=code,
        ),
    )
    if result.outcome is not SendOutcome.SENT:
        log.warning("whatsapp_opt_in_failed", error=result.error)
        raise HTTPException(
            status_code=502, detail="Could not send the verification code."
        )

    return {"status": "sent", "expires_in_minutes": settings.NOTIFY_LINK_TOKEN_TTL_MINUTES}


@router.post("/notifications/destinations/whatsapp/verify")
async def whatsapp_verify(request: Request, payload: VerifyRequest) -> dict:
    """Redeem the code, and only then record the number as a destination."""
    _user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        redeemed = await redeem_token(session, payload.code, CHANNEL_WHATSAPP)
        if redeemed is None or redeemed.address is None:
            raise HTTPException(
                status_code=400, detail="That code is invalid or has expired."
            )
        destination_id = await create_destination(
            session,
            redeemed.tenant_id,
            redeemed.user_id,
            CHANNEL_WHATSAPP,
            redeemed.address,
        )

    return {"status": "verified", "destination_id": str(destination_id)}


@router.delete("/notifications/destinations/{destination_id}", status_code=204)
async def delete_destination(request: Request, destination_id: uuid.UUID) -> None:
    """Unlink. Subscriptions cascade with the destination."""
    _user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        deleted = (
            await session.execute(_DELETE_DESTINATION, {"id": destination_id})
        ).one_or_none()
    if deleted is None:
        raise HTTPException(status_code=404, detail="Destination not found.")
```

This needs one more setting — the bot's public username, which is not derivable from the token. Add to `app/core/config.py` beside the other Telegram keys, and to `.env.example` as `TELEGRAM_BOT_USERNAME=`:

```python
    # The public @name, used to build the t.me deep link. Not derivable from
    # the token, and a wrong one produces a link to somebody else's bot.
    TELEGRAM_BOT_USERNAME: str = ""
```

- [ ] **Step 3b: Add the tenant-scope endpoint**

The spec calls for destinations belonging to the *agency* as well as to a person — an ops feed everyone's job orders reach. The model already allows it (`user_id` nullable) but nothing yet creates one, so the capability would ship unreachable.

Add this test to `tests/test_notifications_api.py`:

```python
async def test_a_destination_can_be_promoted_to_the_whole_agency(
    client, signed_in, admin_session
) -> None:
    tenant_id, user_id = signed_in
    dest_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'x', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.commit()

    response = client.put(
        f"/api/notifications/destinations/{dest_id}/scope", json={"scope": "tenant"}
    )
    assert response.status_code == 200

    body = client.get("/api/notifications/settings").json()
    assert body["destinations"][0]["scope"] == "tenant"


async def test_scope_cannot_reach_another_tenants_destination(
    client, signed_in, admin_session
) -> None:
    other_tenant, other_dest = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'other')"),
        {"id": other_tenant},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :ch, 'x', :hash, now())"
        ),
        {
            "id": other_dest,
            "tid": other_tenant,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("88888"),
        },
    )
    await admin_session.commit()
    try:
        response = client.put(
            f"/api/notifications/destinations/{other_dest}/scope",
            json={"scope": "tenant"},
        )
        assert response.status_code == 404
    finally:
        await admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": other_tenant}
        )
        await admin_session.commit()
```

Run: `uv run pytest tests/test_notifications_api.py -k scope -v`
Expected: FAIL with 405 (the route does not exist)

Add to `app/api/notifications.py`, beside the other statements:

```python
_SET_SCOPE = text(
    """
    UPDATE notification_destinations
    SET user_id = :user_id
    WHERE id = :id
    RETURNING id
    """
)
```

And the endpoint:

```python
class ScopeUpdate(BaseModel):
    # "user" means only the person who linked it; "tenant" means the agency's
    # shared feed, which everyone's job orders reach.
    scope: Literal["user", "tenant"]


@router.put("/notifications/destinations/{destination_id}/scope")
async def set_scope(
    request: Request, destination_id: uuid.UUID, payload: ScopeUpdate
) -> dict:
    """Point a destination at the agency rather than at one recruiter.

    Promoting sets `user_id` to null, which is what the dispatch query reads as
    "this belongs to the tenant". Demoting reattaches it to whoever is asking —
    the only person we can attribute it to from here.
    """
    user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        updated = (
            await session.execute(
                _SET_SCOPE,
                {
                    "id": destination_id,
                    "user_id": None if payload.scope == "tenant" else user_id,
                },
            )
        ).one_or_none()
    if updated is None:
        raise HTTPException(status_code=404, detail="Destination not found.")
    return {"status": "updated", "scope": payload.scope}
```

Add `from typing import Literal` to the imports.

Run: `uv run pytest tests/test_notifications_api.py -v`
Expected: all pass

- [ ] **Step 4: Register the router**

In `app/main.py`, add `notifications` to the `from app.api import ...` line and add below the other includes:

```python
api.include_router(notifications.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_notifications_api.py tests/test_routing.py -v`
Expected: all pass. `test_routing.py` matters here — a route that escapes `/api` is shadowed by the static mount and 404s in production while passing locally.

- [ ] **Step 6: Commit**

```bash
git add app/api/notifications.py app/main.py app/core/config.py .env.example tests/test_notifications_api.py
git commit -m "Let a recruiter choose which job orders reach their phone"
```

---

## Task 10: Telegram webhook

**Files:**
- Create: `app/api/telegram_webhook.py`
- Modify: `app/main.py`
- Test: `tests/test_telegram_webhook.py`

**Interfaces:**
- Consumes: linking (Task 8)
- Produces: `router` with `POST /webhooks/telegram`

- [ ] **Step 1: Write the failing test**

Create `tests/test_telegram_webhook.py`:

```python
"""The bot endpoint. Its URL is public, so the secret header is the whole gate."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.main import app
from app.models.notification import CHANNEL_TELEGRAM
from app.services.notify.linking import issue_token

HEADER = "X-Telegram-Bot-Api-Secret-Token"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
async def account(admin_session):
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'agency')"), {"id": tenant_id}
    )
    await admin_session.execute(
        text("INSERT INTO users (id, tenant_id, email) VALUES (:id, :tid, 'r@a.sg')"),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.commit()
    yield tenant_id, user_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def _update(token: str, chat_id: int = 555) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id, "type": "private"},
            "text": f"/start {token}",
        },
    }


def test_a_missing_secret_is_rejected(client) -> None:
    response = client.post("/api/webhooks/telegram", json=_update("anything"))
    assert response.status_code == 401


def test_a_wrong_secret_is_rejected(client) -> None:
    response = client.post(
        "/api/webhooks/telegram",
        json=_update("anything"),
        headers={HEADER: "not-the-secret"},
    )
    assert response.status_code == 401


async def test_a_valid_start_creates_a_verified_destination(
    client, account, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    tenant_id, user_id = account
    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    response = client.post(
        "/api/webhooks/telegram", json=_update(token), headers={HEADER: "s3cret"}
    )
    assert response.status_code == 200

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT channel, verified_at FROM notification_destinations "
                    "WHERE user_id = :uid"
                ),
                {"uid": user_id},
            )
        ).one()
    assert row.channel == CHANNEL_TELEGRAM
    assert row.verified_at is not None


async def test_an_unknown_token_creates_nothing(client, account, monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    tenant_id, _ = account
    response = client.post(
        "/api/webhooks/telegram",
        json=_update("not-a-real-token"),
        headers={HEADER: "s3cret"},
    )
    # 200 regardless: Telegram retries a non-2xx, and there is nothing to retry.
    assert response.status_code == 200
    async with tenant_session(tenant_id) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM notification_destinations")
            )
        ).scalar_one()
    assert count == 0


async def test_a_message_that_is_not_a_start_is_ignored(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    response = client.post(
        "/api/webhooks/telegram",
        json={
            "update_id": 2,
            "message": {"message_id": 2, "chat": {"id": 1}, "text": "hello"},
        },
        headers={HEADER: "s3cret"},
    )
    assert response.status_code == 200


async def test_a_malformed_update_does_not_500(client, monkeypatch) -> None:
    """Telegram retries a 5xx, so a crash here becomes a retry loop."""
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
    response = client.post(
        "/api/webhooks/telegram", json={"update_id": 3}, headers={HEADER: "s3cret"}
    )
    assert response.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telegram_webhook.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.telegram_webhook'`

- [ ] **Step 3: Write the webhook**

Create `app/api/telegram_webhook.py`:

```python
"""Telegram bot updates (spec 2026-07-28).

Follows `graph_webhook.py`: validate, do the smallest durable thing, answer
fast. Every path returns 200 once the secret checks out — Telegram retries a
non-2xx, and none of the failures here are worth retrying.
"""

import secrets

from fastapi import APIRouter, Header, HTTPException, Request

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.notification import CHANNEL_TELEGRAM
from app.services.notify.linking import create_destination, redeem_token

log = get_logger(__name__)

router = APIRouter(tags=["webhooks"])

_START = "/start"


@router.post("/webhooks/telegram")
async def telegram_update(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, str]:
    """Resolve a `/start <token>` into a verified destination.

    This URL is public and unauthenticated by construction — Telegram will not
    carry a cookie — so the shared secret is the entire gate. Compared with
    `compare_digest`, because a plain `==` leaks its answer through timing.
    """
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected or not x_telegram_bot_api_secret_token:
        raise HTTPException(status_code=401, detail="Unauthorised.")
    if not secrets.compare_digest(x_telegram_bot_api_secret_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorised.")

    payload = await request.json()
    message = payload.get("message") or {}
    text_value = message.get("text") or ""
    chat_id = (message.get("chat") or {}).get("id")

    if not text_value.startswith(_START) or chat_id is None:
        # Someone talking to the bot. There is nothing for us in it.
        return {"status": "ignored"}

    parts = text_value.split(maxsplit=1)
    if len(parts) < 2:
        return {"status": "ignored"}
    token = parts[1].strip()

    # The token names its own tenant, which is the only way this request has of
    # knowing one — there is no session here.
    async with _session_for_token(token) as scoped:
        if scoped is None:
            log.info("telegram_link_token_unknown")
            return {"status": "ignored"}
        session, redeemed = scoped
        await create_destination(
            session,
            redeemed.tenant_id,
            redeemed.user_id,
            CHANNEL_TELEGRAM,
            str(chat_id),
        )

    log.info("telegram_destination_linked")
    return {"status": "linked"}
```

The token must be resolved before a tenant is known, but `redeem_token` runs under RLS, which needs one. Add this helper to the same module:

```python
from contextlib import asynccontextmanager

from sqlalchemy import text

from app.db.session import SessionLocal
from app.models.notification import address_digest

# allow-hardcode: SQL statement, not a phrase list.
#
# Unscoped on purpose, and the only unscoped read in this module. A `/start`
# arrives with no session and no tenant, so something has to map the token to
# one before RLS can be applied. It selects the tenant id and nothing else —
# no user data crosses this query — and everything after it runs inside
# `tenant_session`.
_TENANT_FOR_TOKEN = text(
    """
    SELECT tenant_id FROM notification_link_tokens
    WHERE token_hash = :token_hash
      AND channel = :channel
      AND consumed_at IS NULL
      AND expires_at > now()
    """
)


@asynccontextmanager
async def _session_for_token(token: str):
    """A tenant-scoped session for the token's owner, or None."""
    async with SessionLocal() as lookup:
        tenant_id = (
            await lookup.execute(
                _TENANT_FOR_TOKEN,
                {"token_hash": address_digest(token), "channel": CHANNEL_TELEGRAM},
            )
        ).scalar_one_or_none()

    if tenant_id is None:
        yield None
        return

    async with tenant_session(tenant_id) as session:
        redeemed = await redeem_token(session, token, CHANNEL_TELEGRAM)
        if redeemed is None:
            yield None
            return
        yield session, redeemed
```

> **Implementer note:** `_TENANT_FOR_TOKEN` runs on `SessionLocal` with no tenant set, so the RLS policy filters it to zero rows. The `notification_link_tokens` policy must therefore be readable for this one lookup. Add a second policy in the migration from Task 2 rather than weakening `tenant_isolation`:
> ```sql
> CREATE POLICY token_lookup ON notification_link_tokens
>   FOR SELECT USING (nullif(current_setting('app.tenant_id', true), '') IS NULL);
> ```
> This grants a read *only* when no tenant is set — that is, only to the unscoped webhook path, never to a request that has already claimed a tenant. Add a test in Task 2 asserting a scoped session still sees only its own tokens.

- [ ] **Step 4: Register the router**

In `app/main.py`, add `telegram_webhook` to the `from app.api import ...` line and:

```python
api.include_router(telegram_webhook.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_telegram_webhook.py tests/test_routing.py -v`
Expected: 6 passed plus routing

- [ ] **Step 6: Commit**

```bash
git add app/api/telegram_webhook.py app/main.py alembic/versions/20260728_1000_notifications.py tests/test_telegram_webhook.py
git commit -m "Let pressing start be the proof"
```

---

## Task 11: WhatsApp webhook

**Files:**
- Create: `app/api/whatsapp_webhook.py`
- Modify: `app/main.py`
- Test: `tests/test_whatsapp_webhook.py`

**Interfaces:**
- Consumes: models (Task 2)
- Produces: `router` with `GET /webhooks/whatsapp` (Meta's verification handshake) and `POST /webhooks/whatsapp` (statuses and inbound messages)

- [ ] **Step 1: Write the failing test**

Create `tests/test_whatsapp_webhook.py`:

```python
"""Delivery outcomes and opt-outs arrive here. Without it, nothing disables."""

import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import settings
from app.main import app
from app.models.notification import address_digest

SECRET = "app-secret"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "verify-me")
    return TestClient(app)


def _signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    digest = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def test_verification_handshake_echoes_the_challenge(client) -> None:
    response = client.get(
        "/api/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 200
    assert response.text == "12345"


def test_verification_with_a_wrong_token_is_refused(client) -> None:
    response = client.get(
        "/api/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 403


def test_an_unsigned_post_is_rejected(client) -> None:
    response = client.post("/api/webhooks/whatsapp", json={"entry": []})
    assert response.status_code == 401


def test_a_wrongly_signed_post_is_rejected(client) -> None:
    raw, headers = _signed({"entry": []})
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    response = client.post("/api/webhooks/whatsapp", content=raw, headers=headers)
    assert response.status_code == 401


async def test_a_stop_message_suppresses_the_number(client, admin_session) -> None:
    """Meta's opt-out is per phone number, and we share one across tenants."""
    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": "6591234567", "text": {"body": "STOP"}}
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw, headers = _signed(body)
    assert client.post("/api/webhooks/whatsapp", content=raw, headers=headers).status_code == 200

    count = (
        await admin_session.execute(
            text(
                "SELECT count(*) FROM whatsapp_suppressions WHERE address_hash = :h"
            ),
            {"h": address_digest("+6591234567")},
        )
    ).scalar_one()
    assert count == 1

    await admin_session.execute(text("DELETE FROM whatsapp_suppressions"))
    await admin_session.commit()


async def test_a_failed_status_records_the_delivery_error(
    client, admin_session
) -> None:
    tenant_id, dest_id, delivery_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name) VALUES (:id, 'agency')"), {"id": tenant_id}
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, 'whatsapp', 'x', :hash, now())"
        ),
        {"id": dest_id, "tid": tenant_id, "hash": address_digest("+6591234567")},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_deliveries "
            "(id, tenant_id, destination_id, event_kind, subject_id, status, "
            " provider_message_id) "
            "VALUES (:id, :tid, :did, 'opportunity.new', :sub, 'sent', 'wamid.ABC')"
        ),
        {
            "id": delivery_id,
            "tid": tenant_id,
            "did": dest_id,
            "sub": uuid.uuid4(),
        },
    )
    await admin_session.commit()

    body = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.ABC",
                                    "status": "failed",
                                    "errors": [{"code": 131026, "title": "undeliverable"}],
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw, headers = _signed(body)
    assert client.post("/api/webhooks/whatsapp", content=raw, headers=headers).status_code == 200

    status = (
        await admin_session.execute(
            text("SELECT status FROM notification_deliveries WHERE id = :id"),
            {"id": delivery_id},
        )
    ).scalar_one()
    assert status == "failed"

    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def test_a_malformed_payload_does_not_500(client) -> None:
    """Meta retries a 5xx and eventually disables the webhook entirely."""
    raw, headers = _signed({"entry": [{"changes": [{}]}]})
    assert client.post("/api/webhooks/whatsapp", content=raw, headers=headers).status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_whatsapp_webhook.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.api.whatsapp_webhook'`

- [ ] **Step 3: Write the webhook**

Create `app/api/whatsapp_webhook.py`:

```python
"""Meta delivery statuses and opt-outs (spec 2026-07-28).

Nothing else tells us a message failed. The send call returns 200 when Meta
*accepts* a message, not when it arrives — so without this endpoint, "a
permanent failure disables the destination" has no input and never fires.

Every path answers 200 once the signature checks out. Meta retries a non-2xx
and disables a webhook that keeps failing, which would cost us the status feed
entirely.
"""

import hashlib
import hmac

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.notification import address_digest

log = get_logger(__name__)

router = APIRouter(tags=["webhooks"])

# What a person types to be left alone. Matched case-insensitively on the whole
# trimmed message, not as a substring — "stop by the office tomorrow" is not an
# opt-out.
# allow-hardcode: recognised opt-out keywords, which are Meta's convention.
_OPT_OUT_WORDS = frozenset({"stop", "unsubscribe", "cancel"})

# allow-hardcode: SQL statements, not a phrase list.

# Unscoped, and correctly so: a suppression is a fact about our shared phone
# number, not about any one agency. See the WhatsAppSuppression docstring.
_SUPPRESS = text(
    """
    INSERT INTO whatsapp_suppressions (id, address_hash, reason)
    VALUES (gen_random_uuid(), :address_hash, :reason)
    ON CONFLICT (address_hash) DO NOTHING
    """
)

# Also unscoped. A status callback names a provider message id and no tenant,
# and the id is ours — it came back from a send we made.
_FAIL_DELIVERY = text(
    """
    UPDATE notification_deliveries
    SET status = 'failed', error = :error
    WHERE provider_message_id = :provider_message_id
    RETURNING destination_id
    """
)

_DISABLE_DESTINATION = text(
    "UPDATE notification_destinations SET disabled_at = now() WHERE id = :id"
)


@router.get("/webhooks/whatsapp")
async def verify(request: Request) -> Response:
    """Meta's one-time subscription handshake.

    It expects the raw challenge back as text. Returning JSON fails the
    handshake with no explanation on either side.
    """
    params = request.query_params
    if params.get("hub.verify_token") != settings.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Verification failed.")
    return Response(content=params.get("hub.challenge", ""), media_type="text/plain")


@router.post("/webhooks/whatsapp")
async def receive(request: Request) -> dict[str, str]:
    """Statuses and inbound messages.

    The signature is over the raw body, so the body is read as bytes before
    anything parses it — re-serialising the parsed JSON would change the bytes
    and the digest with them.
    """
    raw = await request.body()
    _verify_signature(raw, request.headers.get("X-Hub-Signature-256"))

    try:
        payload = await request.json()
    except ValueError:
        return {"status": "ignored"}

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            await _handle_statuses(value.get("statuses") or [])
            await _handle_messages(value.get("messages") or [])

    return {"status": "ok"}


def _verify_signature(raw: bytes, header: str | None) -> None:
    secret = settings.WHATSAPP_APP_SECRET
    if not secret or not header or not header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Unauthorised.")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    # compare_digest, because a plain == leaks the answer through timing.
    if not hmac.compare_digest(header.removeprefix("sha256="), expected):
        raise HTTPException(status_code=401, detail="Unauthorised.")


async def _handle_statuses(statuses: list[dict]) -> None:
    for status in statuses:
        if status.get("status") != "failed":
            # `sent`, `delivered`, `read` — the row is already correct.
            continue
        errors = status.get("errors") or [{}]
        detail = f"{errors[0].get('code')}: {errors[0].get('title')}"
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    _FAIL_DELIVERY,
                    {
                        "provider_message_id": status.get("id"),
                        "error": detail[:500],
                    },
                )
            ).one_or_none()
            if row is not None:
                await session.execute(
                    _DISABLE_DESTINATION, {"id": row.destination_id}
                )
            await session.commit()
        log.warning("whatsapp_delivery_failed", error=detail)


async def _handle_messages(messages: list[dict]) -> None:
    for message in messages:
        body = ((message.get("text") or {}).get("body") or "").strip().lower()
        if body not in _OPT_OUT_WORDS:
            continue
        sender = message.get("from")
        if not sender:
            continue
        # Meta reports the sender without the leading +; our addresses carry it.
        async with SessionLocal() as session:
            await session.execute(
                _SUPPRESS,
                {
                    "address_hash": address_digest(f"+{sender.lstrip('+')}"),
                    "reason": "user_stop",
                },
            )
            await session.commit()
        log.info("whatsapp_opt_out_recorded")
```

> **Implementer note:** `_FAIL_DELIVERY` and `_SUPPRESS` run on `SessionLocal` with no tenant set, so RLS filters `notification_deliveries` to zero rows. Add a second policy in the Task 2 migration, on the same principle as the token lookup:
> ```sql
> CREATE POLICY provider_callback ON notification_deliveries
>   FOR ALL USING (nullif(current_setting('app.tenant_id', true), '') IS NULL)
>   WITH CHECK (nullif(current_setting('app.tenant_id', true), '') IS NULL);
> ```
> Granted only when no tenant is set — never to a request that has claimed one. Add a test asserting a scoped session still sees only its own deliveries.

- [ ] **Step 4: Honour suppressions on send**

The suppression table only matters if the send path reads it. In `app/workers/jobs.py`, add beside the other delivery statements:

```python
_IS_SUPPRESSED = text(
    "SELECT 1 FROM whatsapp_suppressions WHERE address_hash = :address_hash"
)
```

In `deliver_notification`, immediately after decrypting the address and before calling the channel:

```python
    address = decrypt(target.address_encrypted)

    if target.channel == CHANNEL_WHATSAPP:
        # Global by design: this person opted out of our *number*, which every
        # tenant shares. Messaging them again through a different agency is
        # exactly what Meta counts against the number, and the rating it moves
        # belongs to everyone.
        async with tenant_session(tenant) as session:
            suppressed = (
                await session.execute(
                    _IS_SUPPRESSED, {"address_hash": address_digest(address)}
                )
            ).one_or_none()
        if suppressed is not None:
            async with tenant_session(tenant) as session:
                await session.execute(
                    _FINISH_DELIVERY,
                    {
                        "id": delivery_id,
                        "status": STATUS_FAILED,
                        "provider_message_id": None,
                        "error": "recipient opted out of WhatsApp messages",
                    },
                )
            return
```

Add `CHANNEL_WHATSAPP` and `address_digest` to the notification imports in `jobs.py`, and add this test to `tests/test_deliver_notification.py`:

```python
async def test_an_opted_out_number_is_never_messaged(
    delivery, admin_session, monkeypatch
) -> None:
    """One agency's opt-out must stop every agency's sends — the reputation
    being spent belongs to a number they all share."""
    from app.core.crypto import encrypt
    from app.models.notification import CHANNEL_WHATSAPP, address_digest

    tenant_id, dest_id, delivery_id = delivery
    await admin_session.execute(
        text(
            "UPDATE notification_destinations "
            "SET channel = :ch, address_encrypted = :enc, address_hash = :hash "
            "WHERE id = :id"
        ),
        {
            "ch": CHANNEL_WHATSAPP,
            "enc": encrypt("+6591234567"),
            "hash": address_digest("+6591234567"),
            "id": dest_id,
        },
    )
    await admin_session.execute(
        text(
            "INSERT INTO whatsapp_suppressions (id, address_hash, reason) "
            "VALUES (gen_random_uuid(), :hash, 'user_stop')"
        ),
        {"hash": address_digest("+6591234567")},
    )
    await admin_session.commit()

    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )
    assert fake.sends == []

    await admin_session.execute(text("DELETE FROM whatsapp_suppressions"))
    await admin_session.commit()
```

- [ ] **Step 5: Register the router**

In `app/main.py`, add `whatsapp_webhook` to the `from app.api import ...` line and:

```python
api.include_router(whatsapp_webhook.router)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_whatsapp_webhook.py tests/test_deliver_notification.py -v`
Expected: 7 + 9 passed

- [ ] **Step 7: Commit**

```bash
git add app/api/whatsapp_webhook.py app/api/../workers/jobs.py app/main.py alembic/versions/20260728_1000_notifications.py tests/test_whatsapp_webhook.py tests/test_deliver_notification.py
git commit -m "Let one agency's STOP stop every agency"
```

---

## Task 12: Wire the producer

**Files:**
- Modify: `app/services/ingest/persist.py`
- Test: `tests/test_notify_producer.py`

**Interfaces:**
- Consumes: `emit`, `enqueue_deliveries` (Task 5), `OpportunityEvent` (Task 3)
- Produces: nothing new — this is the last wire

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_producer.py`:

```python
"""The ingestion pipeline emits, and knows nothing more than that."""

import inspect

from app.services.ingest import persist


def test_persist_emits_notifications() -> None:
    source = inspect.getsource(persist)
    assert "emit" in source


def test_persist_does_not_know_about_channels() -> None:
    """If this fails, the abstraction has leaked into ingestion and adding a
    third channel means editing the extraction pipeline."""
    source = inspect.getsource(persist)
    assert "telegram" not in source.lower()
    assert "whatsapp" not in source.lower()


def test_notifications_are_enqueued_after_the_transaction_commits() -> None:
    """A job that starts before its row is visible reads nothing and exits."""
    source = inspect.getsource(persist.persist)
    emit_at = source.index("await emit(")
    enqueue_at = source.index("enqueue_deliveries")
    # The enqueue must appear after the `async with tenant_session` block ends.
    assert emit_at < enqueue_at
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_notify_producer.py -v`
Expected: FAIL with `AssertionError` on `test_persist_emits_notifications`

- [ ] **Step 3: Emit from persist**

Read `app/services/ingest/persist.py:116-171` to find the `persist()` function and the `async with tenant_session(tenant_id) as session:` block that wraps its writes.

Add the imports:

```python
from app.services.notify.dispatch import emit, enqueue_deliveries
from app.services.notify.events import (
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_NEW,
    OpportunityEvent,
)
```

Inside the existing `async with tenant_session(tenant_id) as session:` block, after every opportunity has been inserted and before the block ends, add:

```python
        # Inside the same transaction that created the opportunity, so either
        # both commit or neither does. A notification for a job order that
        # rolled back is a message about something that never happened.
        for opportunity_id, job in created:
            state = quality_state(job, source)
            delivery_ids.extend(
                await emit(
                    OpportunityEvent(
                        kind=(
                            EVENT_OPPORTUNITY_NEEDS_REVIEW
                            if state == "needs_review"
                            else EVENT_OPPORTUNITY_NEW
                        ),
                        tenant_id=tenant_id,
                        opportunity_id=opportunity_id,
                        # Raw, not normalised: this is what a recruiter
                        # recognises, and the message is read by a person.
                        job_title=_value(job.job_title),
                        company_name=_value(job.company_name),
                        location=_value(job.location),
                        salary=_value(job.salary),
                    ),
                    session,
                )
            )
```

Declare `delivery_ids: list[uuid.UUID] = []` before the `async with` block, and after it — outside the transaction — add:

```python
    # Outside the transaction, deliberately. Redis cannot join it, and a job
    # that starts before its row is committed reads nothing and exits without
    # retrying. `enqueue` fails soft; `flush_notifications` is what turns a
    # lost job back into a queued one.
    await enqueue_deliveries(tenant_id, delivery_ids)
```

> **Implementer note:** `created` above stands for whatever local structure `persist()` already builds pairing each new opportunity id with its `ExtractedJob`. Read the function first and use the real names — if it does not currently keep that pairing, add it rather than re-querying, since the ids are generated in that function.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_notify_producer.py tests/test_extract_job.py -v`
Expected: all pass. `test_extract_job.py` matters — it exercises the persist path end to end and will catch a broken transaction boundary.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: all pass, no errors

- [ ] **Step 6: Lint**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add app/services/ingest/persist.py tests/test_notify_producer.py
git commit -m "Tell someone when a job order lands"
```

---

## Deployment checklist

Not code, and not optional. `CLAUDE.md` records `GRAPH_BASE_URL` missing on one service for a day — harmless until the first code path needed it, then every request 500ed.

- [ ] Apply every `TELEGRAM_*`, `WHATSAPP_*`, and `NOTIFY_*` variable to **both** the `api` and worker Koyeb services. Verify with:

```bash
koyeb deployment get $(koyeb deployments list --service <id> -o json | jq -r '.deployments[0].id') -o json | jq -r '.deployment.definition.env[].key'
```

- [ ] Submit the three WhatsApp templates for approval and set `WHATSAPP_TEMPLATE_*` to the approved names. Two utility, one authentication. Until they are approved and named here, WhatsApp sends fail with code 132001 and Telegram is unaffected.
- [ ] Register the Telegram webhook with the secret:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" -d "url=https://expressautomate.app/api/webhooks/telegram" -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

- [ ] Subscribe the Meta app to `messages` webhooks pointing at `https://expressautomate.app/api/webhooks/whatsapp`, using `WHATSAPP_VERIFY_TOKEN`.
- [ ] Run `uv run alembic upgrade head` against production before deploying the app image — `verify_rls_enforced()` will refuse to boot against a schema without the new policies.
