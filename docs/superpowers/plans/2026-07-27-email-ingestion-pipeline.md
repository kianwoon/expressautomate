# Email Ingestion Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A connected Outlook mailbox streams its job-order emails into Postgres and R2 — reliably, tenant-isolated, and recoverable after any failure — ready for the extraction plan to consume.

**Architecture:** Microsoft Graph change notifications hit an unauthenticated FastAPI webhook, which resolves the tenant through a `SECURITY DEFINER` function, writes an `email_messages` row, and enqueues an arq job. A separate arq worker fetches the message from Graph and stores its body in R2. The existing supervisor process gains periodic subscription renewal, delta reconciliation, and stuck-row rescanning.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, asyncpg, Alembic, arq (Redis/Upstash), boto3 or aioboto3 against Cloudflare R2, httpx, structlog, pytest.

**Spec:** [2026-07-27-email-ingestion-design.md](../specs/2026-07-27-email-ingestion-design.md)

**Follow-on plan:** classification, extraction, evidence validation, and retention are a separate plan. This one ends with raw emails safely stored.

## Global Constraints

- **Nothing hardcoded.** Every URL, model name, key, interval, and limit comes from the repo-root `.env` via `app.core.config.settings`. A literal in source is a defect.
- **Every business table carries `tenant_id`** via the `TenantScoped` mixin and gets a FORCE ROW LEVEL SECURITY policy in the same migration. `verify_rls_enforced()` fails startup otherwise — it flags tables structurally, not by name.
- **All API routes live under `/api`.** `tests/test_routing.py` fails otherwise; the static mount at `/` would shadow them.
- **Single file ≤ 1500 lines.**
- **Tests never touch the live database.** `tests/conftest.py` refuses a non-local host.
- **Reuse `tenant_session()`** from `app/db/rls.py` for every tenant-scoped background operation. Do not write a new tenant-context helper.
- **Graph identifiers:** always send `Prefer: IdType="ImmutableId"` on subscription creation and on message fetch.
- Run everything from `backend/`. Lint with `uv run ruff check .` before each commit.

## File Structure

| File | Responsibility |
|---|---|
| `app/models/mailbox.py` | `Mailbox` — connection, scope, delta checkpoint, retention |
| `app/models/graph_subscription.py` | `GraphSubscription` — subscription id, client_state, expiry |
| `app/models/email_message.py` | `EmailMessage` — raw email metadata, three state machines |
| `alembic/versions/*_ingestion_tables.py` | Tables, indexes, RLS policies, `resolve_subscription()` |
| `app/services/graph/client.py` | Graph HTTP: auth header, ImmutableId, 429 handling |
| `app/services/graph/subscriptions.py` | create / renew / delete subscriptions |
| `app/services/graph/delta.py` | delta walk, `source_state` updates, backfill |
| `app/services/storage/r2.py` | put / get / delete a body by deterministic key |
| `app/services/ingest/intake.py` | Insert-or-ignore an `email_messages` row, enqueue |
| `app/api/graph_webhook.py` | `/api/graph/notifications`, `/api/graph/lifecycle` |
| `app/api/mailboxes.py` | Connect a mailbox, choose scope and start date |
| `app/workers/queue.py` | Redis pool and the enqueue helper. Imports nothing from `jobs` |
| `app/workers/jobs.py` | `fetch_email` and friends. Imports `queue.enqueue` |
| `app/workers/settings.py` | arq `WorkerSettings`. The only module importing both |
| `app/workers/tasks.py` | `renew_subscriptions`, `delta_sync`, `rescan_stuck` |

---

### Task 0: Fix the scope configuration

`.env` was split into `MS_IDENTITY_SCOPES` and `MS_MAILBOX_SCOPES`, but
`app/core/config.py:62` still reads `MS_GRAPH_SCOPES`, which no longer exists.
It falls back to `""`, so `graph_scopes` returns `[]` and `delegated_scopes()`
requests nothing. Sign-in still works — Entra issues an ID token regardless —
so this fails silently today and would surface as a 403 on the first mail call
in Task 6.

**Files:**
- Modify: `app/core/config.py:62`, `app/core/config.py:100-101`, `app/services/ms_auth.py:34-35`
- Test: `tests/test_scopes.py`

**Interfaces:**
- Produces: `settings.identity_scopes -> list[str]`, `settings.mailbox_scopes -> list[str]`,
  `ms_auth.delegated_scopes(kind: str) -> list[str]` where `kind` is `"identity"` or `"mailbox"`

- [ ] **Step 1: Write the failing test**

`tests/test_scopes.py`:

```python
"""The scope configuration must match what .env actually declares.

This test exists because it did not: .env was split into two keys while the
code still read a single one that no longer existed, and every sign-in
silently requested no permissions at all.
"""

from pathlib import Path

import pytest

from app.core.config import Settings, settings
from app.services.ms_auth import delegated_scopes

ENV = Path(__file__).resolve().parents[2] / ".env"


def test_the_settings_match_the_keys_env_declares():
    env_text = ENV.read_text()
    declared = set(Settings.model_fields)

    for key in ("MS_IDENTITY_SCOPES", "MS_MAILBOX_SCOPES"):
        assert f"{key}=" in env_text, f"{key} missing from .env"
        assert key in declared, f"{key} missing from Settings"

    assert "MS_GRAPH_SCOPES" not in declared, "the single-key form is gone from .env"


def test_identity_scopes_are_not_empty():
    assert settings.identity_scopes, "sign-in would request no permissions"


def test_mailbox_scopes_include_mail_read():
    """Graph requires Mail.Read to subscribe to message change notifications."""
    assert any(s.lower() == "mail.read" for s in settings.mailbox_scopes)


def test_identity_consent_does_not_ask_for_mail():
    """Incremental consent: a user who only signs in never sees a mail prompt."""
    assert not any("mail" in s.lower() for s in delegated_scopes("identity"))


def test_msal_reserved_scopes_are_stripped():
    """MSAL injects these itself and errors if they are passed in."""
    for reserved in ("openid", "profile", "offline_access"):
        assert reserved not in delegated_scopes("identity")


def test_an_unknown_scope_kind_is_rejected():
    with pytest.raises(ValueError):
        delegated_scopes("everything")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scopes.py -v`
Expected: FAIL — `MS_IDENTITY_SCOPES missing from Settings`

- [ ] **Step 3: Replace the setting**

In `app/core/config.py`, replace `MS_GRAPH_SCOPES` (line 62) with:

```python
    # Split deliberately: identity is requested at sign-in, mailbox access only
    # when a mailbox is connected. A user who just signs in is never shown a
    # "read your mail" prompt for a capability they have not asked for.
    MS_IDENTITY_SCOPES: str = ""
    MS_MAILBOX_SCOPES: str = ""
```

Replace the `_non_empty_when_configured` validator's target and the
`graph_scopes` property (lines 100-101) with:

```python
    @property
    def identity_scopes(self) -> list[str]:
        return [s for s in self.MS_IDENTITY_SCOPES.split() if s]

    @property
    def mailbox_scopes(self) -> list[str]:
        return [s for s in self.MS_MAILBOX_SCOPES.split() if s]
```

- [ ] **Step 4: Update `delegated_scopes`**

In `app/services/ms_auth.py`, replace lines 34-35:

```python
def delegated_scopes(kind: str = "identity") -> list[str]:
    """Scopes to request for one consent step.

    MSAL adds openid/profile/offline_access itself and raises if they are
    passed in explicitly, so they are stripped here even though `.env` lists
    them as the app registration's permissions.
    """
    if kind == "identity":
        requested = settings.identity_scopes
    elif kind == "mailbox":
        requested = settings.mailbox_scopes
    else:
        raise ValueError(f"unknown scope kind: {kind!r}")
    return [s for s in requested if s.lower() not in _MSAL_RESERVED_SCOPES]
```

Update the two call sites in `begin_login` and `complete_login` to
`delegated_scopes("identity")`.

- [ ] **Step 5: Migrate `tests/test_auth.py`**

Sign-in *behaviour* is unchanged, but five lines in the existing suite name the
old single-key world and will fail:

| Line | Now | Change to |
|---|---|---|
| 66 | `monkeypatch.setattr(settings, "MS_GRAPH_SCOPES", "openid profile email User.Read Mail.Read offline_access")` | Two `setattr` calls: `MS_IDENTITY_SCOPES` = `"openid profile email User.Read offline_access"`, `MS_MAILBOX_SCOPES` = `"Mail.Read"` |
| 81, 92 | `ms_auth.delegated_scopes()` | `ms_auth.delegated_scopes("identity")` |
| 386 | `assert mailbox["scopes"] == ms_auth.delegated_scopes()` | `== ms_auth.delegated_scopes("identity")` — sign-in now stores identity scopes only |
| 569-571 | `scopes = set(ms_auth.delegated_scopes())`, asserting `{"User.Read", "Mail.Read"} <= scopes` | Assert `"User.Read"` is in `delegated_scopes("identity")` and `"Mail.Read"` is in `delegated_scopes("mailbox")` |

`monkeypatch.setattr` raises `AttributeError` on an attribute that no longer
exists, so line 66 fails the whole fixture-dependent suite, not just its own
test. Do this in the same commit as the config change.

- [ ] **Step 6: Extract `_store_refresh_token`**

`microsoft_callback` encrypts and upserts the refresh token inline
(`app/api/auth.py:329-346`). Mailbox consent needs the identical write, and two
copies of a token-encryption path is one too many. Lift it as-is:

```python
async def _store_refresh_token(session, tenant_uuid, user_id, oid, tid, result) -> None:
    """Upsert the encrypted refresh token for one user.

    Called by both consent steps. Entra's consent is cumulative per user and
    app, so the mailbox grant's token is a superset of the identity one and
    overwriting is correct, not lossy.
    """
    refresh_token = result.get("refresh_token")
    if not refresh_token:
        return
    ciphertext = encrypt(refresh_token)
    await session.execute(
        pg_insert(MicrosoftToken)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_uuid,
            user_id=user_id,
            home_account_id=f"{oid}.{tid}",
            refresh_token_encrypted=ciphertext,
            scope=result.get("scope"),
        )
        .on_conflict_do_update(
            constraint="uq_ms_tokens_tenant_user",
            set_={
                "refresh_token_encrypted": ciphertext,
                "scope": result.get("scope"),
                "updated_at": func.now(),
            },
        )
    )
```

Replace the inline block in `microsoft_callback` with a call to it.

- [ ] **Step 7: Run the tests**

```bash
uv run pytest tests/test_scopes.py tests/test_auth.py -v
```

Expected: PASS — the migrated auth tests and the six new scope tests.

- [ ] **Step 8: Commit**

```bash
git add app/core/config.py app/services/ms_auth.py app/api/auth.py tests/test_scopes.py tests/test_auth.py
git commit -m "Read the scope keys that .env actually declares"
```

---

### Task 1: Ingestion tables, RLS, and the subscription resolver

**Files:**
- Create: `app/models/mailbox.py`, `app/models/graph_subscription.py`, `app/models/email_message.py`
- Create: `alembic/versions/20260727_1600_ingestion_tables.py`
- Modify: `app/models/__init__.py`
- Test: `tests/test_ingestion_schema.py`

**Interfaces:**
- Consumes: `Base`, `UUIDPrimaryKey`, `TenantScoped`, `Timestamps` from `app.db.base`
- Produces: `Mailbox`, `GraphSubscription`, `EmailMessage` ORM classes; SQL function `resolve_subscription(text)` returning `(tenant_id uuid, mailbox_id uuid, client_state text)`

- [ ] **Step 1: Write the failing test**

`tests/test_ingestion_schema.py`:

```python
"""Schema-level guarantees for the ingestion tables.

Uses admin_session where a constraint must be observed directly — RLS would
otherwise hide a violation behind an empty result set.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session


@pytest.fixture
async def tenant(admin_session):
    tid = uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
        {"i": tid, "n": "Test Agency", "s": f"test-{tid.hex[:8]}"},
    )
    await admin_session.commit()
    yield tid
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


@pytest.fixture
async def mailbox(admin_session, tenant):
    mid = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'ms-user', 'inbox-folder', 'whole_inbox',"
            " 'active', 24)"
        ),
        {"i": mid, "t": tenant},
    )
    await admin_session.commit()
    return mid


async def test_duplicate_graph_message_id_in_same_mailbox_is_rejected(
    admin_session, tenant, mailbox
):
    for _ in range(2):
        await admin_session.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
                " processing_status, source_state, classification_status)"
                " VALUES (:i, :t, :m, 'AAA', 'pending', 'present', 'unknown')"
            ),
            {"i": uuid.uuid4(), "t": tenant, "m": mailbox},
        )
    with pytest.raises(IntegrityError):
        await admin_session.commit()
    await admin_session.rollback()


async def test_same_internet_message_id_survives_in_a_second_mailbox(
    admin_session, tenant
):
    """Two recruiters CC'd on one email must each keep their own row."""
    boxes = []
    for n in range(2):
        mid = uuid.uuid4()
        await admin_session.execute(
            text(
                "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope,"
                " status, retention_months) VALUES (:i, :t, :u, 'f', 'folder', 'active', 24)"
            ),
            {"i": mid, "t": tenant, "u": f"ms-user-{n}"},
        )
        boxes.append(mid)
    for mid in boxes:
        await admin_session.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
                " internet_message_id, processing_status, source_state,"
                " classification_status) VALUES (:i, :t, :m, :g, '<shared@example.com>',"
                " 'pending', 'present', 'unknown')"
            ),
            {"i": uuid.uuid4(), "t": tenant, "m": mid, "g": f"AAA-{mid.hex[:6]}"},
        )
    await admin_session.commit()
    count = (
        await admin_session.execute(
            text(
                "SELECT count(*) FROM email_messages"
                " WHERE internet_message_id = '<shared@example.com>'"
            )
        )
    ).scalar_one()
    assert count == 2


async def test_resolve_subscription_works_without_tenant_context(
    admin_session, tenant, mailbox
):
    """The webhook has no tenant context; the resolver must still answer."""
    await admin_session.execute(
        text(
            "INSERT INTO graph_subscriptions (id, tenant_id, mailbox_id, subscription_id,"
            " resource, client_state, expires_at, status) VALUES (:i, :t, :m, 'sub-1',"
            " '/me/mailFolders/x/messages', 'secret-1', now() + interval '1 day', 'active')"
        ),
        {"i": uuid.uuid4(), "t": tenant, "m": mailbox},
    )
    await admin_session.commit()

    from app.db.session import SessionLocal

    async with SessionLocal() as session:  # runtime role, no app.tenant_id set
        row = (
            await session.execute(
                text("SELECT * FROM resolve_subscription(:s)"), {"s": "sub-1"}
            )
        ).one()
    assert row.tenant_id == tenant
    assert row.mailbox_id == mailbox
    assert row.client_state == "secret-1"


async def test_email_messages_are_tenant_isolated(admin_session, tenant, mailbox):
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " processing_status, source_state, classification_status)"
            " VALUES (:i, :t, :m, 'BBB', 'pending', 'present', 'unknown')"
        ),
        {"i": uuid.uuid4(), "t": tenant, "m": mailbox},
    )
    await admin_session.commit()

    async with tenant_session(uuid.uuid4()) as other:
        visible = (
            await other.execute(text("SELECT count(*) FROM email_messages"))
        ).scalar_one()
    assert visible == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ingestion_schema.py -v`
Expected: FAIL — `relation "mailboxes" does not exist`

- [ ] **Step 3: Write the models**

`app/models/mailbox.py`:

```python
"""A connected Outlook mailbox (plan §6.2, §8, §9).

One row per mailbox we ingest from. `folder_id` is always populated: Graph's
message delta is folder-scoped, so `whole_inbox` resolves to the well-known
Inbox folder at onboarding rather than being a second mechanism.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Mailbox(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "mailboxes"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    ms_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # whole_inbox | folder
    folder_id: Mapped[str] = mapped_column(String(256), nullable=False)
    folder_name: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Delta tokens are opaque and long; Graph does not bound their length.
    delta_link: Mapped[str | None] = mapped_column(Text)
    initial_sync_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_months: Mapped[int] = mapped_column(Integer, nullable=False)
```

`app/models/graph_subscription.py`:

```python
"""Graph change-notification subscriptions (plan §8).

The routing table: notifications are lean, carrying only a message id and a
subscription id, so this is the sole path from an unauthenticated webhook to a
tenant. It is not exempt from RLS — a `SECURITY DEFINER` resolver function is,
and it returns three columns and nothing else.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class GraphSubscription(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "graph_subscriptions"

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("mailboxes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    # Per subscription, never shared: Graph echoes it on every notification and
    # comparing it is all that stands between a public URL and a forged payload.
    client_state: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
```

`app/models/email_message.py`:

```python
"""Raw email metadata (plan §10, §2.3 as amended).

Bodies live in R2; this row holds everything queryable plus the keys. Three
independent state machines are kept in three columns on purpose:

- `processing_status` — where our pipeline got to
- `source_state`      — what the mailbox looks like now
- `classification_status` — what the relevance gate decided

Graph's message delta is folder-scoped, so an `@removed` event usually means
the recruiter filed the mail elsewhere, not that it was deleted. Collapsing
these into one column silently invalidates opportunities on a folder move.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class EmailMessage(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "mailbox_id", "graph_message_id", name="uq_email_mailbox_graph_id"
        ),
        UniqueConstraint(
            "tenant_id",
            "mailbox_id",
            "internet_message_id",
            name="uq_email_mailbox_internet_id",
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
    has_attachments: Mapped[bool | None] = mapped_column()

    body_r2_key: Mapped[str | None] = mapped_column(Text)
    body_html_r2_key: Mapped[str | None] = mapped_column(Text)

    processing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    source_state: Mapped[str] = mapped_column(String(24), nullable=False, default="present")
    classification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown"
    )
    classification_reason: Mapped[str | None] = mapped_column(Text)
    classification_model: Mapped[str | None] = mapped_column(String(128))
    classification_version: Mapped[str | None] = mapped_column(String(32))

    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
```

Add to `app/models/__init__.py`:

```python
from app.models.email_message import EmailMessage
from app.models.graph_subscription import GraphSubscription
from app.models.mailbox import Mailbox
from app.models.ms_token import MicrosoftToken
from app.models.signup import EarlyAccessSignup
from app.models.tenant import Tenant, User

__all__ = [
    "EarlyAccessSignup",
    "EmailMessage",
    "GraphSubscription",
    "Mailbox",
    "MicrosoftToken",
    "Tenant",
    "User",
]
```

- [ ] **Step 4: Write the migration**

Generate the skeleton, then replace its body:

```bash
uv run alembic revision --autogenerate -m "ingestion tables"
```

Rename the file to `20260727_1600_ingestion_tables.py` and ensure the autogenerated
`upgrade()` for the three tables is followed by this RLS and resolver block. Keep the
autogenerated `op.create_table` calls; append:

```python
PROTECTED = [
    ("mailboxes", "tenant_id"),
    ("graph_subscriptions", "tenant_id"),
    ("email_messages", "tenant_id"),
]

SETTING = "app.tenant_id"


def _apply_rls() -> None:
    role = settings.DATABASE_APP_ROLE
    for table, column in PROTECTED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING "
            f"({column} = current_setting('{SETTING}', true)::uuid)"
        )
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO \"{role}\""
        )


def _create_resolver() -> None:
    """The one pre-tenant read path, deliberately narrow.

    SECURITY DEFINER runs as the migration role, which bypasses RLS — so the
    body must expose exactly the three routing columns and nothing else, and
    the search_path is pinned so the function cannot be tricked into resolving
    `graph_subscriptions` to an attacker-created table.
    """
    role = settings.DATABASE_APP_ROLE
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_subscription(p_subscription_id text)
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid, client_state text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT s.tenant_id, s.mailbox_id, s.client_state
            FROM graph_subscriptions s
            WHERE s.subscription_id = p_subscription_id
              AND s.status = 'active'
        $$
        """
    )
    op.execute(f'GRANT EXECUTE ON FUNCTION resolve_subscription(text) TO "{role}"')
```

Call both at the end of `upgrade()`:

```python
    _apply_rls()
    _create_resolver()
```

And in `downgrade()`, before the table drops:

```python
    op.execute("DROP FUNCTION IF EXISTS resolve_subscription(text)")
```

Import `settings` at the top of the migration:

```python
from app.core.config import settings
```

- [ ] **Step 5: Apply the migration and run the tests**

```bash
uv run alembic upgrade head
uv run pytest tests/test_ingestion_schema.py tests/test_rls.py -v
```

Expected: PASS. `tests/test_rls.py` must still pass — it asserts no readable table lacks FORCE RLS, so a missing policy on any of the three new tables fails here.

- [ ] **Step 6: Commit**

```bash
git add app/models alembic/versions tests/test_ingestion_schema.py
git commit -m "Add the ingestion tables and their tenant policies"
```

---

### Task 2: Graph HTTP client

**Files:**
- Create: `app/services/graph/__init__.py`, `app/services/graph/client.py`
- Test: `tests/test_graph_client.py`

**Interfaces:**
- Consumes: `settings` from `app.core.config`
- Produces:
  - `class GraphClient(token: str, transport: httpx.AsyncBaseTransport | None = None)`
  - `async GraphClient.get(path: str, params: dict | None = None) -> dict`
  - `async GraphClient.post(path: str, json: dict) -> dict`
  - `async GraphClient.patch(path: str, json: dict) -> dict`
  - `async GraphClient.delete(path: str) -> None`
  - `class GraphNotFound(Exception)`, `class GraphThrottled(Exception)` with `.retry_after: float`

- [ ] **Step 1: Write the failing test**

`tests/test_graph_client.py`:

```python
import httpx
import pytest

from app.services.graph.client import GraphClient, GraphNotFound, GraphThrottled


def _client(handler) -> GraphClient:
    return GraphClient(token="fake-token", transport=httpx.MockTransport(handler))


async def test_immutable_id_header_is_always_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["prefer"] = request.headers.get("Prefer")
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"id": "AAA"})

    result = await _client(handler).get("/me/messages/AAA")

    assert result == {"id": "AAA"}
    assert seen["prefer"] == 'IdType="ImmutableId"'
    assert seen["auth"] == "Bearer fake-token"


async def test_404_raises_graph_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})

    with pytest.raises(GraphNotFound):
        await _client(handler).get("/me/messages/GONE")


async def test_429_raises_with_retry_after_from_the_header():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "17"}, json={})

    with pytest.raises(GraphThrottled) as excinfo:
        await _client(handler).get("/me/messages")

    assert excinfo.value.retry_after == 17.0


async def test_429_without_a_header_falls_back_to_the_configured_default():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    with pytest.raises(GraphThrottled) as excinfo:
        await _client(handler).get("/me/messages")

    from app.core.config import settings

    assert excinfo.value.retry_after == float(settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.graph'`

- [ ] **Step 3: Add the settings**

In `app/core/config.py`, inside `Settings`, after the Microsoft block:

```python
    # --- Microsoft Graph ---
    GRAPH_BASE_URL: str = ""
    GRAPH_TIMEOUT_SECONDS: float = 30.0
    GRAPH_DEFAULT_RETRY_AFTER_SECONDS: float = 10.0
    GRAPH_MAX_CONCURRENCY_PER_MAILBOX: int = 4
    GRAPH_SUBSCRIPTION_RENEW_MARGIN: float = 0.5
```

Add to the repo-root `.env`:

```bash
GRAPH_BASE_URL=https://graph.microsoft.com/v1.0
GRAPH_TIMEOUT_SECONDS=30
GRAPH_DEFAULT_RETRY_AFTER_SECONDS=10
GRAPH_MAX_CONCURRENCY_PER_MAILBOX=4
GRAPH_SUBSCRIPTION_RENEW_MARGIN=0.5
```

- [ ] **Step 4: Write the client**

`app/services/graph/__init__.py`: empty file.

`app/services/graph/client.py`:

```python
"""Thin Microsoft Graph HTTP client.

Deliberately thin: it owns authentication, the immutable-ID preference, and
turning Graph's two interesting failure codes into exceptions the callers can
branch on. Everything else — retries, backoff, concurrency — belongs to the
job layer, which knows whether a retry is worth paying for.
"""

import httpx

from app.core.config import settings

IMMUTABLE_ID_HEADER = 'IdType="ImmutableId"'


class GraphError(Exception):
    """Any non-success Graph response that is not modelled more precisely."""


class GraphNotFound(GraphError):
    """The resource is gone. Never worth retrying."""


class GraphThrottled(GraphError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Graph throttled the request; retry after {retry_after}s")
        self.retry_after = retry_after


class GraphClient:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.GRAPH_BASE_URL,
            timeout=settings.GRAPH_TIMEOUT_SECONDS,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                # Graph message ids change when a message moves folders, which
                # would silently break dedup. Immutable ids do not.
                "Prefer": IMMUTABLE_ID_HEADER,
            },
        )

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict | None = None) -> dict:
        return self._unwrap(await self._client.get(path, params=params))

    async def post(self, path: str, json: dict) -> dict:
        return self._unwrap(await self._client.post(path, json=json))

    async def patch(self, path: str, json: dict) -> dict:
        return self._unwrap(await self._client.patch(path, json=json))

    async def delete(self, path: str) -> None:
        response = await self._client.delete(path)
        if response.status_code not in (200, 204, 404):
            self._unwrap(response)

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict:
        if response.status_code == 404:
            raise GraphNotFound(response.text)
        if response.status_code == 429 or response.status_code >= 500:
            raise GraphThrottled(_retry_after(response))
        response.raise_for_status()
        return response.json() if response.content else {}


def _retry_after(response: httpx.Response) -> float:
    """Honour Retry-After when Graph sends it; fall back to a configured default."""
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(settings.GRAPH_DEFAULT_RETRY_AFTER_SECONDS)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_graph_client.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 6: Commit**

```bash
git add app/services/graph tests/test_graph_client.py app/core/config.py
git commit -m "Add a Graph client that survives throttling and moved messages"
```

---

### Task 3: R2 body storage

**Files:**
- Create: `app/services/storage/__init__.py`, `app/services/storage/r2.py`
- Test: `tests/test_r2_storage.py`

**Interfaces:**
- Consumes: `settings`
- Produces:
  - `def body_key(tenant_id, mailbox_id, message_id, kind: str) -> str` where `kind` is `"txt"` or `"html"`
  - `class BodyStore` with `async put(key: str, content: str) -> None`, `async get(key: str) -> str | None`, `async delete(*keys: str) -> None`
  - `class InMemoryBodyStore(BodyStore)` for tests

- [ ] **Step 1: Write the failing test**

`tests/test_r2_storage.py`:

```python
import uuid

from app.services.storage.r2 import InMemoryBodyStore, body_key


def test_body_key_is_deterministic_and_tenant_scoped():
    tenant, mailbox = uuid.uuid4(), uuid.uuid4()

    first = body_key(tenant, mailbox, "AAA-immutable", "txt")
    second = body_key(tenant, mailbox, "AAA-immutable", "txt")

    assert first == second, "a retry must overwrite, never orphan"
    assert first.startswith(f"{tenant}/{mailbox}/")
    assert first.endswith(".txt")


def test_body_key_survives_ids_containing_url_unsafe_characters():
    """Graph immutable ids are base64url-ish and can carry - and _."""
    key = body_key(uuid.uuid4(), uuid.uuid4(), "AAkAL-g_w==", "html")
    assert "/" not in key.rsplit("/", 1)[-1].replace(".html", "")


async def test_in_memory_store_round_trips_and_deletes():
    store = InMemoryBodyStore()
    await store.put("k", "hello")

    assert await store.get("k") == "hello"

    await store.delete("k")
    assert await store.get("k") is None


async def test_deleting_a_missing_key_is_not_an_error():
    """purge_expired reruns after partial failure; it must be idempotent."""
    await InMemoryBodyStore().delete("never-existed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_r2_storage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.storage'`

- [ ] **Step 3: Add the dependency and settings**

```bash
uv add aioboto3
```

In `app/core/config.py`:

```python
    # --- Object storage (Cloudflare R2) ---
    R2_ENDPOINT_URL: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = ""
```

- [ ] **Step 4: Write the store**

`app/services/storage/__init__.py`: empty file.

`app/services/storage/r2.py`:

```python
"""Source-email body storage on Cloudflare R2 (plan §2.3 as amended).

Keys are derived, never generated: the same message always maps to the same
key, so a retried fetch overwrites its own object instead of leaving an orphan
nothing points at. That property is what makes the fetch job safe to retry
without a cleanup pass.
"""

import base64
import uuid
from typing import Protocol

import aioboto3

from app.core.config import settings


def body_key(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, message_id: str, kind: str
) -> str:
    """Deterministic object key for one message body.

    The Graph immutable id is base64-encoded rather than used raw: it may
    contain `/` and `=`, and a `/` would silently invent a key prefix, putting
    two messages' bodies in different logical folders.
    """
    encoded = base64.urlsafe_b64encode(message_id.encode()).decode().rstrip("=")
    return f"{tenant_id}/{mailbox_id}/{encoded}.{kind}"


class BodyStore(Protocol):
    async def put(self, key: str, content: str) -> None: ...
    async def get(self, key: str) -> str | None: ...
    async def delete(self, *keys: str) -> None: ...


class R2BodyStore:
    def __init__(self) -> None:
        self._session = aioboto3.Session()

    def _client(self):
        return self._session.client(
            "s3",
            endpoint_url=settings.R2_ENDPOINT_URL,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        )

    async def put(self, key: str, content: str) -> None:
        async with self._client() as s3:
            await s3.put_object(
                Bucket=settings.R2_BUCKET_NAME, Key=key, Body=content.encode()
            )

    async def get(self, key: str) -> str | None:
        async with self._client() as s3:
            try:
                obj = await s3.get_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
            except s3.exceptions.NoSuchKey:
                return None
            return (await obj["Body"].read()).decode()

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        async with self._client() as s3:
            # delete_objects is idempotent: absent keys are reported as deleted,
            # which is what a rerun of purge_expired needs.
            await s3.delete_objects(
                Bucket=settings.R2_BUCKET_NAME,
                Delete={"Objects": [{"Key": k} for k in keys]},
            )


class InMemoryBodyStore:
    """Test double. The pipeline tests must not reach the network."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    async def put(self, key: str, content: str) -> None:
        self.objects[key] = content

    async def get(self, key: str) -> str | None:
        return self.objects.get(key)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.objects.pop(key, None)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_r2_storage.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 6: Commit**

```bash
git add app/services/storage tests/test_r2_storage.py app/core/config.py pyproject.toml uv.lock
git commit -m "Store email bodies in R2 under derived keys"
```

---

### Task 4: Queue plumbing

**Files:**
- Create: `app/workers/queue.py`
- Test: `tests/test_queue.py`

**Interfaces:**
- Consumes: `settings`
- Produces:
  - `async def enqueue(name: str, **kwargs) -> bool` — returns False rather than raising if Redis is unreachable
  - `async def redis_pool()` — arq `ArqRedis`
  - `class WorkerSettings` — arq entrypoint, functions registered in Task 6

- [ ] **Step 1: Write the failing test**

`tests/test_queue.py`:

```python
from app.workers import queue


async def test_enqueue_returns_false_when_redis_is_unreachable(monkeypatch):
    """The webhook must still return 202 and let rescan_stuck recover.

    A raised exception here would 500 the webhook, and Graph would retry the
    notification — but the row is already committed, so the retry is wasted and
    the user sees nothing. Failing soft plus the rescan sweep is the design.
    """

    async def boom():
        raise ConnectionError("no redis")

    monkeypatch.setattr(queue, "redis_pool", boom)

    assert await queue.enqueue("fetch_email", email_message_id="x") is False


async def test_enqueue_returns_true_on_success(monkeypatch):
    calls = []

    class FakePool:
        async def enqueue_job(self, name, **kwargs):
            calls.append((name, kwargs))

    async def pool():
        return FakePool()

    monkeypatch.setattr(queue, "redis_pool", pool)

    assert await queue.enqueue("fetch_email", email_message_id="x") is True
    assert calls == [("fetch_email", {"email_message_id": "x"})]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.workers.queue'`

- [ ] **Step 3: Add the dependency and settings**

```bash
uv add arq
```

In `app/core/config.py`:

```python
    # --- Queue (Upstash Redis; billed per command, so the poll is slow on purpose) ---
    REDIS_URL: str = ""
    ARQ_POLL_DELAY_SECONDS: float = 2.0
    ARQ_MAX_JOBS: int = 10
    ARQ_MAX_TRIES: int = 5
```

In `.env`:

```bash
ARQ_POLL_DELAY_SECONDS=2.0
ARQ_MAX_JOBS=10
ARQ_MAX_TRIES=5
```

- [ ] **Step 4: Write the queue module**

`app/workers/queue.py`:

```python
"""arq queue plumbing.

Upstash bills per command and arq polls, so `ARQ_POLL_DELAY_SECONDS` is
deliberately slow — latency here costs a couple of seconds, and a tight poll
costs money every second of every day the system is idle.
"""

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.REDIS_URL)


async def redis_pool():
    return await create_pool(redis_settings())


async def enqueue(name: str, **kwargs) -> bool:
    """Enqueue a job, reporting failure rather than raising.

    Redis cannot join the Postgres transaction that just committed the row, so
    a failure here leaves a `pending` row with no job. That is exactly what
    `rescan_stuck` sweeps up. Raising instead would turn a recoverable gap into
    a 500 on a webhook that has already done its durable work.
    """
    try:
        pool = await redis_pool()
        await pool.enqueue_job(name, **kwargs)
        return True
    except Exception:
        log.exception("enqueue_failed", job=name)
        return False
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_queue.py -v`
Expected: PASS — 2 tests.

- [ ] **Step 6: Commit**

```bash
git add app/workers/queue.py tests/test_queue.py app/core/config.py pyproject.toml uv.lock
git commit -m "Add the arq queue, failing soft when Redis is down"
```

---

### Task 5: Webhook endpoint

**Files:**
- Create: `app/api/graph_webhook.py`, `app/services/ingest/__init__.py`, `app/services/ingest/intake.py`
- Modify: `app/main.py` (include the router)
- Test: `tests/test_graph_webhook.py`

**Interfaces:**
- Consumes: `resolve_subscription()` SQL function, `enqueue()` from `app.workers.queue`, `tenant_session()` from `app.db.rls`
- Produces:
  - `router` in `app.api.graph_webhook`, mounted at `/api/graph`
  - `async def record_notification(tenant_id, mailbox_id, graph_message_id) -> uuid.UUID | None` in `app.services.ingest.intake` — returns the row id, or None if it already existed

- [ ] **Step 1: Write the failing test**

`tests/test_graph_webhook.py`:

```python
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
async def subscription(admin_session):
    tid, mid = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tid, "s": f"a-{tid.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
        ),
        {"i": mid, "t": tid},
    )
    await admin_session.execute(
        text(
            "INSERT INTO graph_subscriptions (id, tenant_id, mailbox_id, subscription_id,"
            " resource, client_state, expires_at, status) VALUES (:i, :t, :m, 'sub-x', 'r',"
            " 'secret-x', now() + interval '1 day', 'active')"
        ),
        {"i": uuid.uuid4(), "t": tid, "m": mid},
    )
    await admin_session.commit()
    yield tid, mid
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


async def test_validation_token_is_echoed_as_plain_text(client):
    """Graph sends this at subscription create and expects it back verbatim."""
    response = await client.post(
        "/api/graph/notifications", params={"validationToken": "tok en+123"}
    )

    assert response.status_code == 200
    assert response.text == "tok en+123"
    assert response.headers["content-type"].startswith("text/plain")


async def test_wrong_client_state_is_rejected_and_stores_nothing(client, subscription):
    response = await client.post(
        "/api/graph/notifications",
        json={
            "value": [
                {
                    "subscriptionId": "sub-x",
                    "clientState": "not-the-secret",
                    "resourceData": {"id": "MSG-1"},
                }
            ]
        },
    )

    assert response.status_code == 202
    from app.db.rls import tenant_session

    tid, _ = subscription
    async with tenant_session(tid) as session:
        count = (
            await session.execute(text("SELECT count(*) FROM email_messages"))
        ).scalar_one()
    assert count == 0, "a forged notification must not create a row"


async def test_valid_notification_stores_a_pending_row(client, subscription, monkeypatch):
    from app.api import graph_webhook

    enqueued = []

    async def fake_enqueue(name, **kwargs):
        enqueued.append((name, kwargs))
        return True

    monkeypatch.setattr(graph_webhook, "enqueue", fake_enqueue)

    response = await client.post(
        "/api/graph/notifications",
        json={
            "value": [
                {
                    "subscriptionId": "sub-x",
                    "clientState": "secret-x",
                    "resourceData": {"id": "MSG-1"},
                }
            ]
        },
    )

    assert response.status_code == 202
    tid, _ = subscription
    from app.db.rls import tenant_session

    async with tenant_session(tid) as session:
        row = (
            await session.execute(
                text(
                    "SELECT graph_message_id, processing_status FROM email_messages"
                )
            )
        ).one()
    assert row.graph_message_id == "MSG-1"
    assert row.processing_status == "pending"
    assert enqueued[0][0] == "fetch_email"


async def test_duplicate_notification_creates_one_row(client, subscription, monkeypatch):
    from app.api import graph_webhook

    async def fake_enqueue(name, **kwargs):
        return True

    monkeypatch.setattr(graph_webhook, "enqueue", fake_enqueue)

    payload = {
        "value": [
            {
                "subscriptionId": "sub-x",
                "clientState": "secret-x",
                "resourceData": {"id": "MSG-DUP"},
            }
        ]
    }
    await client.post("/api/graph/notifications", json=payload)
    await client.post("/api/graph/notifications", json=payload)

    tid, _ = subscription
    from app.db.rls import tenant_session

    async with tenant_session(tid) as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM email_messages WHERE graph_message_id = 'MSG-DUP'"
                )
            )
        ).scalar_one()
    assert count == 1


async def test_unknown_subscription_is_accepted_but_ignored(client):
    """Graph retries anything non-2xx; a stale subscription would retry forever."""
    response = await client.post(
        "/api/graph/notifications",
        json={
            "value": [
                {
                    "subscriptionId": "sub-that-we-deleted",
                    "clientState": "whatever",
                    "resourceData": {"id": "MSG-9"},
                }
            ]
        },
    )
    assert response.status_code == 202
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph_webhook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.graph_webhook'`

- [ ] **Step 3: Write the intake service**

`app/services/ingest/__init__.py`: empty file.

`app/services/ingest/intake.py`:

```python
"""Turning a Graph notification into a durable row.

The insert is `ON CONFLICT DO NOTHING` because every recovery path in the
system — webhook retry, delta sync, backfill — is allowed to arrive at the
same message. Replay has to be free, or the recovery layer becomes a source of
duplicates rather than a cure for gaps.
"""

import uuid

from sqlalchemy import text

from app.db.rls import tenant_session


async def record_notification(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, graph_message_id: str
) -> uuid.UUID | None:
    """Insert a `pending` row. Returns its id, or None if it already existed."""
    row_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        result = await session.execute(
            text(
                """
                INSERT INTO email_messages
                    (id, tenant_id, mailbox_id, graph_message_id,
                     processing_status, source_state, classification_status)
                VALUES (:id, :tenant, :mailbox, :graph_id,
                        'pending', 'present', 'unknown')
                ON CONFLICT (tenant_id, mailbox_id, graph_message_id) DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": row_id,
                "tenant": tenant_id,
                "mailbox": mailbox_id,
                "graph_id": graph_message_id,
            },
        )
        return result.scalar_one_or_none()
```

- [ ] **Step 4: Write the webhook**

`app/api/graph_webhook.py`:

```python
"""Microsoft Graph change-notification endpoints (plan §7, §8).

Two rules govern everything here:

1. **Answer fast.** Graph gives roughly three seconds before it treats the
   notification as failed. So this endpoint does one insert and one enqueue,
   and never fetches, never calls a model.
2. **Answer 2xx.** Graph retries non-2xx responses for hours. A notification we
   cannot act on — unknown subscription, forged clientState — is accepted and
   dropped, because retrying it would never start working.
"""

import hmac

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.services.ingest.intake import record_notification
from app.workers.queue import enqueue

log = get_logger(__name__)

router = APIRouter(prefix="/graph", tags=["graph"])


def _validation_response(request: Request) -> Response | None:
    """Graph's subscription handshake: echo the token, verbatim, as text.

    Returning JSON here fails the handshake — the token must come back as
    text/plain and unmodified, which is why it is read from the raw query
    string rather than parsed and re-encoded.
    """
    token = request.query_params.get("validationToken")
    if token is None:
        return None
    return Response(content=token, media_type="text/plain", status_code=200)


async def _resolve(subscription_id: str) -> tuple | None:
    """Map a subscription id to its tenant without any tenant context.

    Uses the SECURITY DEFINER resolver, which is the only pre-tenant read path
    in the system and returns three routing columns and nothing else.
    """
    async with SessionLocal() as session:
        return (
            await session.execute(
                text("SELECT * FROM resolve_subscription(:s)"),
                {"s": subscription_id},
            )
        ).one_or_none()


@router.post("/notifications")
async def notifications(request: Request) -> Response:
    if (validation := _validation_response(request)) is not None:
        return validation

    payload = await request.json()
    for item in payload.get("value", []):
        subscription_id = item.get("subscriptionId", "")
        record = await _resolve(subscription_id)
        if record is None:
            log.warning("notification_unknown_subscription", subscription=subscription_id)
            continue
        # Constant-time: the comparison is the only thing protecting a public URL.
        if not hmac.compare_digest(item.get("clientState") or "", record.client_state):
            log.warning("notification_bad_client_state", subscription=subscription_id)
            continue

        message_id = (item.get("resourceData") or {}).get("id")
        if not message_id:
            continue

        row_id = await record_notification(record.tenant_id, record.mailbox_id, message_id)
        if row_id is None:
            continue  # Already known. Replay is free by design.
        await enqueue("fetch_email", email_message_id=str(row_id))

    return Response(status_code=202)


@router.post("/lifecycle")
async def lifecycle(request: Request) -> Response:
    """Subscription health events (plan §8).

    Without this endpoint a revoked grant shows up only as notifications
    quietly stopping, which looks exactly like a slow week.
    """
    if (validation := _validation_response(request)) is not None:
        return validation

    payload = await request.json()
    for item in payload.get("value", []):
        subscription_id = item.get("subscriptionId", "")
        record = await _resolve(subscription_id)
        if record is None:
            continue

        event = item.get("lifecycleEvent")
        if event == "reauthorizationRequired":
            await enqueue("reauthorize_subscription", subscription_id=subscription_id)
        elif event == "subscriptionRemoved":
            await enqueue("recreate_subscription", mailbox_id=str(record.mailbox_id))
        elif event == "missed":
            # Notifications were dropped on Graph's side. Reconcile now rather
            # than waiting up to ten minutes for the scheduled sweep.
            await enqueue("delta_sync_mailbox", mailbox_id=str(record.mailbox_id))
        else:
            log.warning("lifecycle_unknown_event", event=event)

    return Response(status_code=202)
```

- [ ] **Step 5: Mount the router**

In `app/main.py`, alongside the existing router includes:

```python
from app.api import graph_webhook

app.include_router(graph_webhook.router, prefix="/api")
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_graph_webhook.py tests/test_routing.py -v`
Expected: PASS — 5 webhook tests plus routing. `test_routing.py` confirms both new routes live under `/api`.

- [ ] **Step 7: Commit**

```bash
git add app/api/graph_webhook.py app/services/ingest app/main.py tests/test_graph_webhook.py
git commit -m "Accept Graph notifications and record them durably"
```

---

### Task 6: The fetch_email job

**Files:**
- Create: `app/workers/jobs.py`
- Modify: `app/workers/queue.py` (register functions on `WorkerSettings`)
- Test: `tests/test_fetch_email_job.py`

**Interfaces:**
- Consumes: `GraphClient`, `GraphNotFound`, `GraphThrottled`, `BodyStore`, `body_key`, `tenant_session`
- Produces:
  - `async def fetch_email(ctx, email_message_id: str) -> None`
  - `async def access_token_for_mailbox(mailbox_id: uuid.UUID) -> str` in `app/services/ms_auth.py` (added here)

- [ ] **Step 1: Write the failing test**

`tests/test_fetch_email_job.py`:

```python
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.services.graph.client import GraphClient
from app.services.storage.r2 import InMemoryBodyStore, body_key
from app.workers import jobs

GRAPH_MESSAGE = {
    "id": "MSG-1",
    "internetMessageId": "<abc@example.com>",
    "conversationId": "CONV-1",
    "subject": "Finance officer — KLN Logistics",
    "receivedDateTime": "2026-07-27T02:15:00Z",
    "hasAttachments": False,
    "from": {"emailAddress": {"name": "Evelyn Xie", "address": "evelynxie@example.com"}},
    "body": {"contentType": "html", "content": "<p>Up to $3500</p>"},
    "bodyPreview": "Up to $3500",
}


@pytest.fixture
async def pending_row(admin_session):
    tid, mid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tid, "s": f"a-{tid.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
        ),
        {"i": mid, "t": tid},
    )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " processing_status, source_state, classification_status)"
            " VALUES (:i, :t, :m, 'MSG-1', 'pending', 'present', 'unknown')"
        ),
        {"i": eid, "t": tid, "m": mid},
    )
    await admin_session.commit()
    yield tid, mid, eid
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


def _patch(monkeypatch, store, handler):
    async def fake_client(tenant_id, mailbox_id):
        return GraphClient(token="t", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", fake_client)
    monkeypatch.setattr(jobs, "body_store", lambda: store)

    async def fake_enqueue(name, **kwargs):
        return True

    monkeypatch.setattr(jobs, "enqueue", fake_enqueue)


async def test_fetch_stores_body_and_metadata(monkeypatch, admin_session, pending_row):
    tid, mid, eid = pending_row
    store = InMemoryBodyStore()
    _patch(monkeypatch, store, lambda r: httpx.Response(200, json=GRAPH_MESSAGE))

    await jobs.fetch_email({}, email_message_id=str(eid))

    row = (
        await admin_session.execute(
            text(
                "SELECT processing_status, sender_email, subject, received_datetime,"
                " body_html_r2_key, internet_message_id FROM email_messages WHERE id = :i"
            ),
            {"i": eid},
        )
    ).one()
    assert row.processing_status == "fetched"
    assert row.sender_email == "evelynxie@example.com"
    assert row.received_datetime is not None, "the screenshot's missing column"
    assert row.internet_message_id == "<abc@example.com>"
    assert store.objects[body_key(tid, mid, "MSG-1", "html")] == "<p>Up to $3500</p>"


async def test_body_lands_in_r2_before_the_status_flips(
    monkeypatch, admin_session, pending_row
):
    """A crash between the two must never leave a row pointing at nothing."""
    tid, mid, eid = pending_row
    store = InMemoryBodyStore()

    class ExplodingStore(InMemoryBodyStore):
        async def put(self, key, content):
            raise RuntimeError("R2 down")

    _patch(monkeypatch, ExplodingStore(), lambda r: httpx.Response(200, json=GRAPH_MESSAGE))

    with pytest.raises(RuntimeError):
        await jobs.fetch_email({}, email_message_id=str(eid))

    status = (
        await admin_session.execute(
            text("SELECT processing_status FROM email_messages WHERE id = :i"), {"i": eid}
        )
    ).scalar_one()
    assert status == "pending", "must stay retryable, not advance past a failed write"


async def test_404_marks_the_row_unfetchable_and_does_not_retry(
    monkeypatch, admin_session, pending_row
):
    _, _, eid = pending_row
    _patch(monkeypatch, InMemoryBodyStore(), lambda r: httpx.Response(404, json={}))

    await jobs.fetch_email({}, email_message_id=str(eid))

    row = (
        await admin_session.execute(
            text(
                "SELECT processing_status, source_state FROM email_messages WHERE id = :i"
            ),
            {"i": eid},
        )
    ).one()
    assert row.processing_status == "unfetchable"
    assert row.source_state == "deleted"


async def test_throttling_defers_the_job_and_leaves_the_row_pending(
    monkeypatch, admin_session, pending_row
):
    """arq reschedules on Retry and only on Retry — a bare exception is a
    failed job, and Graph's Retry-After would be thrown away."""
    from arq import Retry

    _, _, eid = pending_row
    _patch(
        monkeypatch,
        InMemoryBodyStore(),
        lambda r: httpx.Response(429, headers={"Retry-After": "5"}, json={}),
    )

    with pytest.raises(Retry) as excinfo:
        await jobs.fetch_email({}, email_message_id=str(eid))

    assert excinfo.value.defer_score == 5000  # arq stores the defer in ms

    status = (
        await admin_session.execute(
            text("SELECT processing_status FROM email_messages WHERE id = :i"), {"i": eid}
        )
    ).scalar_one()
    assert status == "pending"


async def test_already_fetched_row_is_a_no_op(monkeypatch, admin_session, pending_row):
    """Duplicate enqueue from rescan_stuck must not refetch."""
    _, _, eid = pending_row
    await admin_session.execute(
        text("UPDATE email_messages SET processing_status = 'fetched' WHERE id = :i"),
        {"i": eid},
    )
    await admin_session.commit()

    def explode(request):
        raise AssertionError("Graph must not be called for an already-fetched row")

    _patch(monkeypatch, InMemoryBodyStore(), explode)

    await jobs.fetch_email({}, email_message_id=str(eid))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fetch_email_job.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.workers.jobs'`

- [ ] **Step 3: Add token acquisition to `app/services/ms_auth.py`**

```python
import asyncio
import uuid

from sqlalchemy import text

from app.core.crypto import decrypt, encrypt
from app.db.rls import tenant_session


async def access_token_for_mailbox(tenant_id: uuid.UUID, mailbox_id: uuid.UUID) -> str:
    """Exchange the stored refresh token for an access token.

    Serialized per user with a Postgres advisory lock. Two concurrent refreshes
    race to persist their result, and the loser's stored token is stale — the
    lock guards the read-refresh-write sequence, not just the HTTP call.
    """
    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT t.user_id, t.refresh_token_encrypted FROM ms_oauth_tokens t"
                    " JOIN mailboxes m ON m.user_id = t.user_id WHERE m.id = :m"
                ),
                {"m": mailbox_id},
            )
        ).one()
        # hashtext gives a stable bigint key; the lock is transaction-scoped and
        # released on commit or rollback, so a crashed worker cannot hold it.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"ms-refresh:{row.user_id}"},
        )
        result = await asyncio.to_thread(
            client().acquire_token_by_refresh_token,
            decrypt(row.refresh_token_encrypted),
            # Mailbox scopes, not identity: a token minted from a refresh token
            # that only ever carried identity consent will 403 on every mail
            # call, and it will do so at fetch time rather than here.
            scopes=delegated_scopes("mailbox"),
        )
        if "access_token" not in result:
            raise PermissionError(result.get("error_description", "refresh failed"))
        if new_refresh := result.get("refresh_token"):
            await session.execute(
                text(
                    "UPDATE ms_oauth_tokens SET refresh_token_encrypted = :r"
                    " WHERE user_id = :u"
                ),
                {"r": encrypt(new_refresh), "u": row.user_id},
            )
        return result["access_token"]
```

- [ ] **Step 4: Write the job**

`app/workers/jobs.py`:

```python
"""arq jobs (plan §7).

Fetch and extraction are separate jobs so their failure domains stay separate:
a Graph throttle must not cost an LLM call, and a bad model response must not
cost another Graph round trip.
"""

import uuid
from datetime import datetime

from arq import Retry
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.db.session import SessionLocal
from app.services.graph.client import GraphClient, GraphNotFound, GraphThrottled
from app.services.ms_auth import access_token_for_mailbox
from app.services.storage.r2 import R2BodyStore, body_key
from app.workers.queue import enqueue

log = get_logger(__name__)

MESSAGE_FIELDS = (
    "id,internetMessageId,conversationId,subject,receivedDateTime,"
    "hasAttachments,from,body,bodyPreview"
)


def body_store():
    """Indirection point so tests can swap in the in-memory store."""
    return R2BodyStore()


async def graph_client_for_mailbox(tenant_id: uuid.UUID, mailbox_id: uuid.UUID) -> GraphClient:
    return GraphClient(token=await access_token_for_mailbox(tenant_id, mailbox_id))


async def _locate(email_message_id: str) -> tuple | None:
    """Find the row's tenant before we have tenant context.

    Only the routing columns, and only for a row that still needs work — this
    is the same narrow pre-tenant read the webhook makes, expressed as a
    primary-key lookup.
    """
    async with SessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT * FROM resolve_email_row(:i)"
                ),
                {"i": email_message_id},
            )
        ).one_or_none()


async def fetch_email(ctx, email_message_id: str) -> None:
    located = await _locate(email_message_id)
    if located is None:
        log.info("fetch_skipped_unknown_row", email_message_id=email_message_id)
        return

    tenant_id, mailbox_id, graph_message_id, status = located
    if status != "pending":
        # rescan_stuck and delta sync may both enqueue the same row. Doing the
        # work twice is wasteful; doing it once is the point of this guard.
        log.info("fetch_skipped_not_pending", email_message_id=email_message_id, status=status)
        return

    client = await graph_client_for_mailbox(tenant_id, mailbox_id)
    try:
        message = await client.get(
            f"/users/{{}}/messages/{graph_message_id}".format(
                await _ms_user_id(tenant_id, mailbox_id)
            ),
            params={"$select": MESSAGE_FIELDS},
        )
    except GraphNotFound:
        # The message vanished before we ever saw its body. That source really
        # is gone; record it as such rather than retrying forever.
        await _mark_unfetchable(tenant_id, email_message_id)
        return
    except GraphThrottled as exc:
        # arq only reschedules on `Retry`; a bare exception is a failed job and
        # `max_tries` never enters into it. Deferring by what Graph asked for is
        # the difference between backing off and hammering a throttled tenant.
        raise Retry(defer=exc.retry_after) from exc
    finally:
        await client.aclose()

    store = body_store()
    html = (message.get("body") or {}).get("content") or ""
    textual = message.get("bodyPreview") or ""
    html_key = body_key(tenant_id, mailbox_id, graph_message_id, "html")
    text_key = body_key(tenant_id, mailbox_id, graph_message_id, "txt")

    # Objects first, status second. A crash between them costs one repeated
    # write on retry; the reverse costs a row pointing at nothing.
    await store.put(html_key, html)
    await store.put(text_key, textual)

    sender = ((message.get("from") or {}).get("emailAddress")) or {}
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                """
                UPDATE email_messages SET
                    internet_message_id = :imid,
                    conversation_id = :conv,
                    sender_name = :sname,
                    sender_email = :semail,
                    subject = :subject,
                    received_datetime = :received,
                    has_attachments = :attach,
                    body_html_r2_key = :hkey,
                    body_r2_key = :tkey,
                    processing_status = 'fetched',
                    attempt_count = attempt_count + 1
                WHERE id = :id
                """
            ),
            {
                "imid": message.get("internetMessageId"),
                "conv": message.get("conversationId"),
                "sname": sender.get("name"),
                "semail": sender.get("address"),
                "subject": message.get("subject"),
                "received": _parse_dt(message.get("receivedDateTime")),
                "attach": message.get("hasAttachments"),
                "hkey": html_key,
                "tkey": text_key,
                "id": email_message_id,
            },
        )

    await enqueue("classify_email", email_message_id=email_message_id)


async def _ms_user_id(tenant_id: uuid.UUID, mailbox_id: uuid.UUID) -> str:
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text("SELECT ms_user_id FROM mailboxes WHERE id = :i"), {"i": mailbox_id}
            )
        ).scalar_one()


async def _mark_unfetchable(tenant_id: uuid.UUID, email_message_id: str) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "UPDATE email_messages SET processing_status = 'unfetchable',"
                " source_state = 'deleted' WHERE id = :i"
            ),
            {"i": email_message_id},
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
```

- [ ] **Step 5: Add the `resolve_email_row` function**

The job needs the same pre-tenant lookup the webhook has, keyed on the row id.
Create `alembic/versions/20260727_1700_resolve_email_row.py`:

```python
"""resolve_email_row

Revision ID: b41d7c9e2a55
Revises: <the ingestion tables revision id>
"""

from alembic import op

from app.core.config import settings

revision = "b41d7c9e2a55"
down_revision = "<the ingestion tables revision id>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_email_row(p_id uuid)
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid, graph_message_id text,
                       processing_status text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT e.tenant_id, e.mailbox_id, e.graph_message_id, e.processing_status
            FROM email_messages e
            WHERE e.id = p_id
        $$
        """
    )
    op.execute(
        f'GRANT EXECUTE ON FUNCTION resolve_email_row(uuid) TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_email_row(uuid)")
```

Apply it:

```bash
uv run alembic upgrade head
```

- [ ] **Step 6: Register the job**

Create `app/workers/settings.py` — a **third** module, not an addition to
`queue.py`. `jobs.py` imports `queue.enqueue`, so a `WorkerSettings` living in
`queue.py` and importing `jobs` makes the two modules mutually dependent, and
whichever is imported first (in tests, `jobs`) fails on a partially initialised
module.

```python
"""arq entrypoint: `uv run arq app.workers.settings.WorkerSettings`.

Deliberately separate from `queue.py`. Jobs import the enqueue helper, so the
registry that imports the jobs has to sit above both or the import graph cycles.
"""

from app.core.config import settings
from app.workers.jobs import fetch_email
from app.workers.queue import redis_settings


class WorkerSettings:
    functions = [fetch_email]
    redis_settings = redis_settings()
    poll_delay = settings.ARQ_POLL_DELAY_SECONDS
    max_jobs = settings.ARQ_MAX_JOBS
    max_tries = settings.ARQ_MAX_TRIES
```

Later tasks append to `functions` here, never in `queue.py`.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_fetch_email_job.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 8: Commit**

```bash
git add app/workers/jobs.py app/workers/queue.py app/services/ms_auth.py alembic/versions tests/test_fetch_email_job.py
git commit -m "Fetch each notified message and store its source"
```

---

### Task 7: Subscription lifecycle

**Files:**
- Create: `app/services/graph/subscriptions.py`
- Create: `alembic/versions/*_client_state_not_empty.py`
- Test: `tests/test_subscriptions.py`

**Carried forward from the Task 5 review:** add
`CHECK (client_state <> '')` to `graph_subscriptions` in a migration here.
`_client_state_matches` in the webhook already refuses an empty secret, and
that is the enforcement point — but this is the task that *writes* the column,
and a row with an empty secret should be impossible to create, not merely
harmless to receive.

**Interfaces:**
- Consumes: `GraphClient`, `tenant_session`, `settings`
- Produces:
  - `async def create_subscription(tenant_id, mailbox_id, ms_user_id, folder_id, client) -> str`
  - `async def renew_subscription(tenant_id, subscription_row_id, client) -> None`
  - `async def due_for_renewal(now: datetime) -> list[tuple]`
  - `def renewal_threshold(created_at, expires_at) -> datetime`

- [ ] **Step 1: Write the failing test**

`tests/test_subscriptions.py`:

```python
from datetime import UTC, datetime, timedelta

import httpx

from app.services.graph.client import GraphClient
from app.services.graph.subscriptions import renewal_threshold


def test_renewal_threshold_is_half_of_the_granted_life():
    created = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
    expires = created + timedelta(days=7)

    assert renewal_threshold(created, expires) == created + timedelta(days=3, hours=12)


def test_renewal_threshold_uses_what_graph_granted_not_a_constant():
    """Graph may grant less than we asked for; renewing on a hardcoded 3 days
    would then miss the window entirely."""
    created = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
    expires = created + timedelta(hours=2)

    assert renewal_threshold(created, expires) == created + timedelta(hours=1)


async def test_create_subscription_sends_a_random_per_subscription_client_state():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "id": "sub-new",
                "expirationDateTime": "2026-08-03T00:00:00Z",
            },
        )

    client = GraphClient(token="t", transport=httpx.MockTransport(handler))
    states = set()
    for _ in range(2):
        states.add(await _client_state_of(client, handler, captured))

    assert len(states) == 2, "each subscription needs its own secret"


async def _client_state_of(client, handler, captured):
    import uuid

    from app.services.graph.subscriptions import build_subscription_payload

    payload = build_subscription_payload(
        ms_user_id="u", folder_id="f", client_state=None
    )
    return payload["clientState"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subscriptions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.graph.subscriptions'`

- [ ] **Step 3: Write the service**

`app/services/graph/subscriptions.py`:

```python
"""Graph subscription lifecycle (plan §8).

Subscriptions expire, and Graph decides when — it may grant less than the
maximum, and the maximum itself has changed over time. So nothing here assumes
a duration: the renewal point is always derived from the `expirationDateTime`
Graph actually returned.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.services.graph.client import GraphClient

log = get_logger(__name__)


def renewal_threshold(created_at: datetime, expires_at: datetime) -> datetime:
    """When to renew: a configured fraction into the granted lifetime."""
    life = expires_at - created_at
    return created_at + life * settings.GRAPH_SUBSCRIPTION_RENEW_MARGIN


def build_subscription_payload(
    ms_user_id: str, folder_id: str, client_state: str | None
) -> dict:
    """One secret per subscription, never a shared one.

    A shared secret makes every tenant's notifications forgeable the moment it
    leaks anywhere; a per-subscription value limits that to one mailbox.
    """
    return {
        "changeType": "created",
        "notificationUrl": settings.MS_WEBHOOK_NOTIFICATION_URL,
        "lifecycleNotificationUrl": settings.MS_WEBHOOK_LIFECYCLE_URL,
        "resource": f"/users/{ms_user_id}/mailFolders/{folder_id}/messages",
        "clientState": client_state or secrets.token_urlsafe(32),
        "expirationDateTime": _requested_expiry(),
        "includeResourceData": False,
    }


def _requested_expiry() -> str:
    minutes = settings.GRAPH_SUBSCRIPTION_REQUEST_MINUTES
    return (
        (datetime.now(UTC) + timedelta(minutes=minutes))
        .isoformat()
        .replace("+00:00", "Z")
    )


async def create_subscription(
    tenant_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    ms_user_id: str,
    folder_id: str,
    client: GraphClient,
) -> str:
    payload = build_subscription_payload(ms_user_id, folder_id, client_state=None)
    created = await client.post("/subscriptions", json=payload)

    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO graph_subscriptions
                    (id, tenant_id, mailbox_id, subscription_id, resource,
                     client_state, expires_at, status)
                VALUES (:id, :tenant, :mailbox, :sub, :resource, :state, :expires, 'active')
                """
            ),
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "mailbox": mailbox_id,
                "sub": created["id"],
                "resource": payload["resource"],
                "state": payload["clientState"],
                # What Graph granted, not what we asked for.
                "expires": datetime.fromisoformat(
                    created["expirationDateTime"].replace("Z", "+00:00")
                ),
            },
        )
    return created["id"]


async def renew_subscription(
    tenant_id: uuid.UUID, subscription_id: str, client: GraphClient
) -> None:
    updated = await client.patch(
        f"/subscriptions/{subscription_id}",
        json={"expirationDateTime": _requested_expiry()},
    )
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "UPDATE graph_subscriptions SET expires_at = :e, last_renewed_at = now(),"
                " status = 'active' WHERE subscription_id = :s"
            ),
            {
                "e": datetime.fromisoformat(
                    updated["expirationDateTime"].replace("Z", "+00:00")
                ),
                "s": subscription_id,
            },
        )
```

Add the setting in `app/core/config.py`:

```python
    GRAPH_SUBSCRIPTION_REQUEST_MINUTES: int = 4230
    MS_WEBHOOK_LIFECYCLE_URL: str = ""
```

And in `.env`:

```bash
GRAPH_SUBSCRIPTION_REQUEST_MINUTES=4230
MS_WEBHOOK_LIFECYCLE_URL=https://expressautomate.app/api/graph/lifecycle
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_subscriptions.py -v`
Expected: PASS — 3 tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/graph/subscriptions.py app/core/config.py tests/test_subscriptions.py
git commit -m "Create and renew Graph subscriptions on what Graph granted"
```

---

### Task 8: Delta sync and source_state

**Files:**
- Create: `app/services/graph/delta.py`
- Test: `tests/test_delta_sync.py`

**Interfaces:**
- Consumes: `GraphClient`, `record_notification`, `tenant_session`, `enqueue`
- Produces:
  - `async def sync_mailbox(tenant_id, mailbox_id, client, *, max_messages: int | None = None) -> int`
  - `async def backfill_mailbox(tenant_id, mailbox_id, client, since: datetime) -> int`

- [ ] **Step 1: Write the failing test**

`tests/test_delta_sync.py`:

```python
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.services.graph.client import GraphClient
from app.services.graph.delta import sync_mailbox


@pytest.fixture
async def mailbox(admin_session):
    tid, mid = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tid, "s": f"a-{tid.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
        ),
        {"i": mid, "t": tid},
    )
    await admin_session.commit()
    yield tid, mid
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


def _pages(*payloads):
    it = iter(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(it))

    return handler


async def test_removed_message_is_marked_moved_not_deleted(
    monkeypatch, admin_session, mailbox
):
    """Graph message delta is folder-scoped: @removed usually means 'filed
    elsewhere', and must not invalidate what we extracted from it."""
    tid, mid = mailbox
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " processing_status, source_state, classification_status)"
            " VALUES (:i, :t, :m, 'MSG-MOVED', 'extracted', 'present', 'recruitment')"
        ),
        {"i": uuid.uuid4(), "t": tid, "m": mid},
    )
    await admin_session.commit()

    from app.services.graph import delta

    async def fake_enqueue(name, **kwargs):
        return True

    monkeypatch.setattr(delta, "enqueue", fake_enqueue)

    client = GraphClient(
        token="t",
        transport=httpx.MockTransport(
            _pages(
                {
                    "value": [{"id": "MSG-MOVED", "@removed": {"reason": "changed"}}],
                    "@odata.deltaLink": "https://graph/delta?token=next",
                }
            )
        ),
    )
    await sync_mailbox(tid, mid, client)

    row = (
        await admin_session.execute(
            text(
                "SELECT processing_status, source_state FROM email_messages"
                " WHERE graph_message_id = 'MSG-MOVED'"
            )
        )
    ).one()
    assert row.source_state == "removed_from_folder"
    assert row.processing_status == "extracted", "the opportunity remains valid"


async def test_new_messages_are_recorded_and_enqueued(monkeypatch, admin_session, mailbox):
    tid, mid = mailbox
    from app.services.graph import delta

    enqueued = []

    async def fake_enqueue(name, **kwargs):
        enqueued.append(name)
        return True

    monkeypatch.setattr(delta, "enqueue", fake_enqueue)

    client = GraphClient(
        token="t",
        transport=httpx.MockTransport(
            _pages(
                {
                    "value": [{"id": "NEW-1"}, {"id": "NEW-2"}],
                    "@odata.deltaLink": "https://graph/delta?token=next",
                }
            )
        ),
    )
    count = await sync_mailbox(tid, mid, client)

    assert count == 2
    assert enqueued == ["fetch_email", "fetch_email"]

    stored = (
        await admin_session.execute(
            text("SELECT delta_link FROM mailboxes WHERE id = :i"), {"i": mid}
        )
    ).scalar_one()
    assert stored == "https://graph/delta?token=next"


async def test_replaying_the_same_delta_page_creates_no_duplicates(
    monkeypatch, admin_session, mailbox
):
    tid, mid = mailbox
    from app.services.graph import delta

    async def fake_enqueue(name, **kwargs):
        return True

    monkeypatch.setattr(delta, "enqueue", fake_enqueue)

    page = {
        "value": [{"id": "SAME"}],
        "@odata.deltaLink": "https://graph/delta?token=next",
    }
    for _ in range(2):
        client = GraphClient(token="t", transport=httpx.MockTransport(_pages(page)))
        await sync_mailbox(tid, mid, client)

    count = (
        await admin_session.execute(
            text("SELECT count(*) FROM email_messages WHERE graph_message_id = 'SAME'")
        )
    ).scalar_one()
    assert count == 1


async def test_max_messages_stops_the_walk(monkeypatch, admin_session, mailbox):
    """Backfill is capped; the UI must not imply unlimited historical import."""
    tid, mid = mailbox
    from app.services.graph import delta

    async def fake_enqueue(name, **kwargs):
        return True

    monkeypatch.setattr(delta, "enqueue", fake_enqueue)

    client = GraphClient(
        token="t",
        transport=httpx.MockTransport(
            _pages(
                {
                    "value": [{"id": f"M-{n}"} for n in range(5)],
                    "@odata.nextLink": "https://graph/delta?page=2",
                }
            )
        ),
    )
    count = await sync_mailbox(tid, mid, client, max_messages=3)

    assert count == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_delta_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.graph.delta'`

- [ ] **Step 3: Write the service**

`app/services/graph/delta.py`:

```python
"""Delta synchronisation and reconciliation (plan §9).

This is the recovery path: webhooks are fast but lossy, and this walk is what
makes a missed notification, a webhook outage, or a Graph incident survivable.
It is also the backfill path — onboarding differs only in where the walk
starts.
"""

import uuid
from datetime import datetime

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.services.graph.client import GraphClient
from app.services.ingest.intake import record_notification
from app.workers.queue import enqueue

log = get_logger(__name__)


async def sync_mailbox(
    tenant_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    client: GraphClient,
    *,
    max_messages: int | None = None,
    since: datetime | None = None,
) -> int:
    """Walk the mailbox delta, recording what we find. Returns messages seen."""
    url, ms_user_id, folder_id = await _walk_start(tenant_id, mailbox_id, since)
    seen = 0
    delta_link: str | None = None

    while url:
        page = await client.get(url)
        for item in page.get("value", []):
            message_id = item.get("id")
            if not message_id:
                continue
            if "@removed" in item:
                await _mark_removed(tenant_id, mailbox_id, message_id)
                continue

            row_id = await record_notification(tenant_id, mailbox_id, message_id)
            seen += 1
            if row_id is not None:
                await enqueue("fetch_email", email_message_id=str(row_id))

            if max_messages is not None and seen >= max_messages:
                # Store the page we stopped on, not nothing: without a
                # checkpoint the next sweep re-walks from the beginning and
                # re-caps at the same place, forever.
                resume = page.get("@odata.nextLink") or page.get("@odata.deltaLink")
                if resume:
                    await _store_delta_link(tenant_id, mailbox_id, resume)
                log.info(
                    "delta_walk_capped",
                    mailbox_id=str(mailbox_id),
                    cap=max_messages,
                )
                return seen

        delta_link = page.get("@odata.deltaLink") or delta_link
        url = page.get("@odata.nextLink")

    if delta_link:
        await _store_delta_link(tenant_id, mailbox_id, delta_link)
    return seen


async def backfill_mailbox(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, client: GraphClient, since: datetime
) -> int:
    """Initial sync (plan §6.2), bounded by both configured limits.

    Graph delta filtered by receivedDateTime is not a bulk export mechanism, so
    the walk stops at whichever limit comes first and the mailbox is marked
    backfilled from there.
    """
    count = await sync_mailbox(
        tenant_id,
        mailbox_id,
        client,
        max_messages=settings.INITIAL_SYNC_MAX_MESSAGES,
        since=since,
    )
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("UPDATE mailboxes SET backfill_completed_at = now() WHERE id = :i"),
            {"i": mailbox_id},
        )
    return count


async def _walk_start(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, since: datetime | None
) -> tuple[str, str, str]:
    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT ms_user_id, folder_id, delta_link FROM mailboxes WHERE id = :i"
                ),
                {"i": mailbox_id},
            )
        ).one()

    if row.delta_link and since is None:
        return row.delta_link, row.ms_user_id, row.folder_id

    base = f"/users/{row.ms_user_id}/mailFolders/{row.folder_id}/messages/delta"
    if since is not None:
        stamp = since.isoformat().replace("+00:00", "Z")
        base = f"{base}?$filter=receivedDateTime ge {stamp}"
    return base, row.ms_user_id, row.folder_id


async def _mark_removed(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, message_id: str
) -> None:
    """A message left the monitored folder.

    Not the same as deletion, and deliberately does not touch
    processing_status: the source is already safe in R2 and the opportunities
    extracted from it remain true.
    """
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "UPDATE email_messages SET source_state = 'removed_from_folder'"
                " WHERE mailbox_id = :m AND graph_message_id = :g"
            ),
            {"m": mailbox_id, "g": message_id},
        )


async def _store_delta_link(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, delta_link: str
) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("UPDATE mailboxes SET delta_link = :d WHERE id = :i"),
            {"d": delta_link, "i": mailbox_id},
        )
```

Add settings in `app/core/config.py`:

```python
    # --- Initial sync limits (plan §6.2) ---
    INITIAL_SYNC_MAX_MESSAGES: int = 5000
    INITIAL_SYNC_MAX_LOOKBACK_DAYS: int = 90
```

And `.env`:

```bash
INITIAL_SYNC_MAX_MESSAGES=5000
INITIAL_SYNC_MAX_LOOKBACK_DAYS=90
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_delta_sync.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/graph/delta.py app/core/config.py tests/test_delta_sync.py
git commit -m "Reconcile mailboxes by delta without losing moved mail"
```

---

### Task 9: Supervisor tasks

**Files:**
- Create: `app/workers/tasks.py`
- Modify: `app/workers/main.py` (`build_tasks`)
- Test: `tests/test_worker_tasks.py`

**Interfaces:**
- Consumes: `SessionLocal`, `enqueue`, `renewal_threshold`
- Produces:
  - `async def rescan_stuck() -> int`
  - `async def renew_subscriptions() -> int`
  - `async def delta_sync_all() -> int`

- [ ] **Step 1: Write the failing test**

`tests/test_worker_tasks.py`:

```python
import uuid

import pytest
from sqlalchemy import text

from app.workers import tasks


@pytest.fixture
async def mailbox(admin_session):
    tid, mid = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tid, "s": f"a-{tid.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope, status,"
            " retention_months) VALUES (:i, :t, 'u', 'f', 'folder', 'active', 24)"
        ),
        {"i": mid, "t": tid},
    )
    await admin_session.commit()
    yield tid, mid
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :i"), {"i": tid})
    await admin_session.commit()


async def _insert(admin_session, tid, mid, gid, status, age_minutes):
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " processing_status, source_state, classification_status, updated_at)"
            " VALUES (:i, :t, :m, :g, :s, 'present', 'unknown',"
            " now() - make_interval(mins => :age))"
        ),
        {"i": uuid.uuid4(), "t": tid, "m": mid, "g": gid, "s": status, "age": age_minutes},
    )
    await admin_session.commit()


@pytest.mark.parametrize(
    "status,age,expected_job",
    [
        ("pending", 10, "fetch_email"),
        ("fetched", 30, "classify_email"),
        ("classifying", 30, "classify_email"),
        ("extracting", 30, "extract_email"),
    ],
)
async def test_stalled_rows_are_re_enqueued(
    monkeypatch, admin_session, mailbox, status, age, expected_job
):
    """Criterion 3: killing a worker at ANY non-terminal status loses nothing."""
    tid, mid = mailbox
    await _insert(admin_session, tid, mid, f"G-{status}", status, age)

    enqueued = []

    async def fake_enqueue(name, **kwargs):
        enqueued.append(name)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)

    await tasks.rescan_stuck()

    assert expected_job in enqueued


@pytest.mark.parametrize("status", ["extracted", "no_opportunity", "skipped",
                                    "unfetchable", "failed"])
async def test_terminal_rows_are_never_re_enqueued(
    monkeypatch, admin_session, mailbox, status
):
    tid, mid = mailbox
    await _insert(admin_session, tid, mid, f"T-{status}", status, 600)

    enqueued = []

    async def fake_enqueue(name, **kwargs):
        enqueued.append(name)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)

    await tasks.rescan_stuck()

    assert enqueued == []


async def test_fresh_rows_are_left_alone(monkeypatch, admin_session, mailbox):
    """A row a worker is actively holding must not be duplicated."""
    tid, mid = mailbox
    await _insert(admin_session, tid, mid, "FRESH", "pending", 1)

    enqueued = []

    async def fake_enqueue(name, **kwargs):
        enqueued.append(name)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)

    await tasks.rescan_stuck()

    assert enqueued == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_tasks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.workers.tasks'`

- [ ] **Step 3: Write the tasks**

`app/workers/tasks.py`:

```python
"""Periodic recovery tasks for the supervisor process (plan §8, §9).

These run in `app/workers/main.py`, not in arq. The split is deliberate: arq
processes work, this process makes sure work exists to be processed. One can
fail without silencing the other.
"""

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.workers.queue import enqueue

log = get_logger(__name__)

# Which job resumes a row stalled in each non-terminal status. The terminal
# statuses are absent on purpose — extracted, no_opportunity, skipped,
# unfetchable and failed are outcomes, not interruptions.
RESUME_JOB = {
    "pending": "fetch_email",
    "fetched": "classify_email",
    "classifying": "classify_email",
    "extracting": "extract_email",
}


async def rescan_stuck() -> int:
    """Re-enqueue rows no worker is going to pick up on its own.

    This is the outbox net: Redis cannot join the Postgres transaction that
    committed the row, so an enqueue that fails after commit leaves durable
    work with no job attached. Without this sweep, success criterion 3 is
    false.

    Runs unscoped by tenant deliberately — it must see every tenant's stalled
    work, so it reads through the admin-owned resolver rather than a tenant
    session, and touches only routing columns.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT * FROM stalled_email_rows(:pending_age, :working_age)"),
                {
                    "pending_age": settings.RESCAN_PENDING_MINUTES,
                    "working_age": settings.RESCAN_WORKING_MINUTES,
                },
            )
        ).all()

    requeued = 0
    for row in rows:
        job = RESUME_JOB.get(row.processing_status)
        if job is None:
            continue
        if await enqueue(job, email_message_id=str(row.id)):
            requeued += 1

    if requeued:
        log.info("rescan_stuck_requeued", count=requeued)
    return requeued


async def renew_subscriptions() -> int:
    """Renew before expiry, using the lifetime Graph actually granted."""
    from app.services.graph.subscriptions import renew_subscription
    from app.workers.jobs import graph_client_for_mailbox

    async with SessionLocal() as session:
        due = (
            await session.execute(
                text("SELECT * FROM subscriptions_due_for_renewal(:margin)"),
                {"margin": settings.GRAPH_SUBSCRIPTION_RENEW_MARGIN},
            )
        ).all()

    renewed = 0
    for row in due:
        client = await graph_client_for_mailbox(row.tenant_id, row.mailbox_id)
        try:
            await renew_subscription(row.tenant_id, row.subscription_id, client)
            renewed += 1
        except Exception:
            log.exception("subscription_renewal_failed", subscription=row.subscription_id)
            await enqueue("recreate_subscription", mailbox_id=str(row.mailbox_id))
        finally:
            await client.aclose()
    return renewed


async def delta_sync_all() -> int:
    """Reconcile every active mailbox."""
    async with SessionLocal() as session:
        mailboxes = (
            await session.execute(text("SELECT * FROM active_mailboxes()"))
        ).all()

    for row in mailboxes:
        await enqueue("delta_sync_mailbox", mailbox_id=str(row.mailbox_id))
    return len(mailboxes)
```

- [ ] **Step 4: Add the three cross-tenant resolver functions**

Create `alembic/versions/20260727_1800_operator_resolvers.py`. These exist for
the same reason `resolve_subscription` does: the supervisor sweeps every
tenant, so it has no single tenant context to set, and a narrow
`SECURITY DEFINER` function is a far smaller exemption than a role that
bypasses RLS.

```python
"""operator resolvers

Revision ID: c52e8d0f3b66
Revises: b41d7c9e2a55
"""

from alembic import op

from app.core.config import settings

revision = "c52e8d0f3b66"
down_revision = "b41d7c9e2a55"
branch_labels = None
depends_on = None

FUNCTIONS = {
    "stalled_email_rows(p_pending_minutes int, p_working_minutes int)": """
        RETURNS TABLE (id uuid, processing_status text)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT e.id, e.processing_status
            FROM email_messages e
            WHERE (e.processing_status = 'pending'
                   AND e.updated_at < now()
                       - make_interval(mins => p_pending_minutes))
               OR (e.processing_status IN ('fetched', 'classifying', 'extracting')
                   AND e.updated_at < now()
                       - make_interval(mins => p_working_minutes))
        $$
    """,
    "subscriptions_due_for_renewal(p_margin double precision)": """
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid, subscription_id text)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT s.tenant_id, s.mailbox_id, s.subscription_id
            FROM graph_subscriptions s
            WHERE s.status = 'active'
              AND now() >= s.created_at + (s.expires_at - s.created_at) * p_margin
        $$
    """,
    "active_mailboxes()": """
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT m.tenant_id, m.id FROM mailboxes m WHERE m.status = 'active'
        $$
    """,
}


def upgrade() -> None:
    role = settings.DATABASE_APP_ROLE
    for signature, body in FUNCTIONS.items():
        op.execute(f"CREATE OR REPLACE FUNCTION {signature} {body}")
        name = signature.split("(")[0]
        args = signature.split("(", 1)[1].rstrip(")")
        types = ", ".join(
            part.strip().split(" ", 1)[1] for part in args.split(",") if part.strip()
        )
        op.execute(f'GRANT EXECUTE ON FUNCTION {name}({types}) TO "{role}"')


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS stalled_email_rows(int, int)")
    op.execute("DROP FUNCTION IF EXISTS subscriptions_due_for_renewal(double precision)")
    op.execute("DROP FUNCTION IF EXISTS active_mailboxes()")
```

Add settings to `app/core/config.py`:

```python
    RESCAN_PENDING_MINUTES: int = 5
    RESCAN_WORKING_MINUTES: int = 15
    RENEW_INTERVAL_SECONDS: float = 900.0
    DELTA_SYNC_INTERVAL_SECONDS: float = 600.0
    RESCAN_INTERVAL_SECONDS: float = 300.0
```

And `.env`:

```bash
RESCAN_PENDING_MINUTES=5
RESCAN_WORKING_MINUTES=15
RENEW_INTERVAL_SECONDS=900
DELTA_SYNC_INTERVAL_SECONDS=600
RESCAN_INTERVAL_SECONDS=300
```

Apply:

```bash
uv run alembic upgrade head
```

- [ ] **Step 5: Register the tasks in the supervisor**

Replace `build_tasks()` and delete `_heartbeat` in `app/workers/main.py`:

```python
def build_tasks() -> list[PeriodicTask]:
    """Registry of periodic work (plan §8, §9).

    arq processes jobs; this process makes sure jobs exist. Keeping them in
    separate processes means a wedged arq worker still gets fresh work queued,
    and a crashed supervisor does not stop work already queued.
    """
    from app.workers.tasks import delta_sync_all, rescan_stuck, renew_subscriptions

    async def _rescan() -> None:
        await rescan_stuck()

    async def _renew() -> None:
        await renew_subscriptions()

    async def _delta() -> None:
        await delta_sync_all()

    return [
        PeriodicTask("rescan_stuck", settings.RESCAN_INTERVAL_SECONDS, _rescan),
        PeriodicTask("renew_subscriptions", settings.RENEW_INTERVAL_SECONDS, _renew),
        PeriodicTask("delta_sync", settings.DELTA_SYNC_INTERVAL_SECONDS, _delta),
    ]
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_worker_tasks.py -v`
Expected: PASS — 10 tests (4 parametrised stalled, 5 parametrised terminal, 1 fresh).

- [ ] **Step 7: Commit**

```bash
git add app/workers/tasks.py app/workers/main.py alembic/versions app/core/config.py tests/test_worker_tasks.py
git commit -m "Recover stalled rows, expiring subscriptions, and missed mail"
```

---

### Task 10: Mailbox onboarding

**Files:**
- Create: `app/api/mailboxes.py`
- Modify: `app/main.py`, `app/workers/queue.py` (register `backfill_mailbox_job`, `delta_sync_mailbox`), `app/workers/jobs.py`
- Test: `tests/test_mailbox_onboarding.py`

**Interfaces:**
- Consumes: `create_subscription`, `backfill_mailbox`, `enqueue`
- Produces:
  - `POST /api/mailboxes/connect` accepting `{scope, folder_id?, start_from}`
  - `async def backfill_mailbox_job(ctx, mailbox_id: str) -> None`
  - `async def delta_sync_mailbox(ctx, mailbox_id: str) -> None`

- [ ] **Step 1: Write the failing test**

`tests/test_mailbox_onboarding.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest

from app.api.mailboxes import ConnectRequest, resolve_start_date
from app.core.config import settings


def test_start_date_is_clamped_to_the_configured_lookback():
    """A custom date a year back must not imply unlimited historical import."""
    requested = datetime.now(UTC) - timedelta(days=365)

    resolved = resolve_start_date(requested)

    earliest = datetime.now(UTC) - timedelta(days=settings.INITIAL_SYNC_MAX_LOOKBACK_DAYS)
    assert resolved >= earliest - timedelta(seconds=5)


def test_a_recent_start_date_is_kept_as_asked():
    requested = datetime.now(UTC) - timedelta(days=3)

    assert resolve_start_date(requested) == requested


def test_whole_inbox_scope_requires_no_folder_id():
    """Both scopes resolve to one folder id; whole_inbox is not a second path."""
    request = ConnectRequest(scope="whole_inbox", start_from=datetime.now(UTC))

    assert request.folder_id is None


def test_folder_scope_without_a_folder_id_is_rejected():
    with pytest.raises(ValueError):
        ConnectRequest(scope="folder", folder_id=None, start_from=datetime.now(UTC))


async def test_connect_asks_for_mailbox_scopes_not_identity_scopes(monkeypatch):
    """The sign-in token cannot read mail, including for users who signed in
    before mailbox scopes existed. Connect must run its own consent."""
    from app.api import mailboxes

    captured = {}

    class FakeMsal:
        def initiate_auth_code_flow(self, scopes, redirect_uri):
            captured["scopes"] = scopes
            captured["redirect_uri"] = redirect_uri
            return {"auth_uri": "https://login.microsoftonline.com/authorize?x=1"}

    monkeypatch.setattr(mailboxes, "client", lambda: FakeMsal())
    monkeypatch.setattr(mailboxes, "_seal_flow", lambda flow: "sealed")

    result = await mailboxes.connect(
        ConnectRequest(scope="whole_inbox", start_from=datetime.now(UTC)),
        user=_FakeUser(),
    )

    assert any(s.lower() == "mail.read" for s in captured["scopes"])
    assert not any("user.read" == s.lower() for s in captured["scopes"])
    assert captured["redirect_uri"] == settings.MS_MAILBOX_REDIRECT_URI
    assert result["authorize_url"].startswith("https://login.microsoftonline.com/")


async def test_connect_creates_no_mailbox_row_before_consent(monkeypatch, admin_session):
    """An abandoned consent screen must not leave a row the renewal sweep
    then tries to subscribe for every fifteen minutes."""
    from sqlalchemy import text

    from app.api import mailboxes

    class FakeMsal:
        def initiate_auth_code_flow(self, scopes, redirect_uri):
            return {"auth_uri": "https://login.microsoftonline.com/authorize"}

    monkeypatch.setattr(mailboxes, "client", lambda: FakeMsal())
    monkeypatch.setattr(mailboxes, "_seal_flow", lambda flow: "sealed")

    before = (
        await admin_session.execute(text("SELECT count(*) FROM mailboxes"))
    ).scalar_one()

    await mailboxes.connect(
        ConnectRequest(scope="whole_inbox", start_from=datetime.now(UTC)),
        user=_FakeUser(),
    )

    after = (
        await admin_session.execute(text("SELECT count(*) FROM mailboxes"))
    ).scalar_one()
    assert after == before


class _FakeUser:
    import uuid as _uuid

    id = _uuid.uuid4()
    tenant_id = _uuid.uuid4()
    ms_object_id = "ms-object-id"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mailbox_onboarding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.mailboxes'`

- [ ] **Step 3: Extract a `current_user` dependency**

`app/api/auth.py` has no reusable dependency — the cookie decoding lives inline
in `me()` at line 377. Lift it, and have `me()` use it, so there is one place
that decides who is signed in:

```python
async def current_user(request: Request) -> User:
    """The signed-in user, or 401.

    Lifted out of `me()` so every authenticated route decodes the session the
    same way. Two copies of this logic is two places for a session-expiry bug.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise HTTPException(status_code=401, detail="Not signed in.")
    try:
        payload = _session_serializer.loads(cookie, max_age=SESSION_TTL_SECONDS)
        tenant_uuid = uuid.UUID(payload["tid"])
        user_uuid = uuid.UUID(payload["uid"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Session is invalid or expired.") from exc

    async with tenant_session(tenant_uuid) as session:
        user = (
            await session.execute(select(User).where(User.id == user_uuid))
        ).scalar_one_or_none()
    if user is None:
        # A deleted user with a live cookie must not look signed in.
        raise HTTPException(status_code=401, detail="Not signed in.")
    return user
```

Then replace the duplicated block at the top of `me()` with
`user = await current_user(request)`, keeping the rest of that handler as is.
Run `uv run pytest tests/test_auth.py -v` — it must still pass unchanged.

- [ ] **Step 4: Write the endpoint**

`app/api/mailboxes.py`:

```python
"""Mailbox onboarding (plan §6.2).

**Two steps, because consent is incremental.** Signing in grants identity
scopes only, so the stored refresh token cannot read mail — including for users
who signed in before mailbox scopes existed. `/connect` therefore starts a
second consent for `MS_MAILBOX_SCOPES` and returns the authorize URL; the work
happens in `/connect/callback` once the user has granted it.

Entra's consent is cumulative per user and app, so after this second grant the
newly stored refresh token can acquire tokens for both scope sets, and the
identity flow is unaffected.

Order matters inside the callback: the subscription is created *before* the
historical walk starts, so a message arriving mid-onboarding is caught by the
subscription, the backfill, or both. The dedup indexes make the overlap free —
the reverse order leaves a gap where it is caught by neither.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, model_validator
from sqlalchemy import text

from app.api.auth import (
    FLOW_TTL_SECONDS,
    _cookie_kwargs,
    _flow_cookie_name,
    _frontend_url,
    _open_flow,
    _seal_flow,
    _stale_flow_cookies,
    _store_refresh_token,
    current_user,
)
from app.core.config import settings
from app.db.rls import tenant_session
from app.services.graph.subscriptions import create_subscription
from app.services.ms_auth import client, delegated_scopes
from app.workers.jobs import graph_client_for_mailbox
from app.workers.queue import enqueue

router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])

WELL_KNOWN_INBOX = "inbox"


class ConnectRequest(BaseModel):
    scope: str  # whole_inbox | folder
    folder_id: str | None = None
    start_from: datetime

    @model_validator(mode="after")
    def _folder_scope_needs_a_folder(self) -> "ConnectRequest":
        if self.scope == "folder" and not self.folder_id:
            raise ValueError("folder_id is required when scope is 'folder'")
        if self.scope not in ("whole_inbox", "folder"):
            raise ValueError("scope must be 'whole_inbox' or 'folder'")
        return self


def resolve_start_date(requested: datetime) -> datetime:
    """Clamp to the configured lookback.

    Graph delta filtered by receivedDateTime is not a bulk export mechanism, so
    the product must not offer what the implementation cannot deliver. Bulk
    historical import is a separate feature.
    """
    earliest = datetime.now(UTC) - timedelta(days=settings.INITIAL_SYNC_MAX_LOOKBACK_DAYS)
    return max(requested, earliest)


@router.post("/connect")
async def connect(payload: ConnectRequest, user=Depends(current_user)) -> dict:
    """Start the mailbox consent. Returns the URL the browser must visit.

    Nothing is created here. A user who abandons the consent screen must not
    leave a `mailboxes` row behind that the renewal sweep then tries to
    subscribe for every fifteen minutes.
    """
    if not settings.microsoft_configured():
        raise HTTPException(status_code=503, detail="Microsoft sign-in is not configured")

    flow = client().initiate_auth_code_flow(
        delegated_scopes("mailbox"),
        redirect_uri=settings.MS_MAILBOX_REDIRECT_URI,
    )
    # The user's onboarding choices ride through the OAuth round trip inside the
    # sealed flow, not in the URL — a folder id in a query string is one more
    # thing that can be tampered with on the way back.
    sealed = _seal_flow({**flow, "onboarding": payload.model_dump(mode="json")})

    response = JSONResponse({"authorize_url": flow["auth_uri"]})
    # Named per flow, exactly as sign-in does: two tabs mid-onboarding must not
    # overwrite each other's flow. Returning the sealed value in the body
    # instead would leave the callback with no cookie to read at all.
    response.set_cookie(
        _flow_cookie_name(flow["state"]), sealed, **_cookie_kwargs(FLOW_TTL_SECONDS)
    )
    for stale in _stale_flow_cookies(request):
        response.delete_cookie(stale, path="/")
    return response


@router.get("/connect/callback")
async def connect_callback(request: Request, user=Depends(current_user)) -> Response:
    """Finish consent, then create the mailbox, subscription, and backfill."""
    state = request.query_params.get("state", "")
    sealed = request.cookies.get(_flow_cookie_name(state))
    if sealed is None:
        # Expired, evicted, or a callback that never had a flow. 400, not the
        # 500 a bare dict lookup would raise.
        raise HTTPException(status_code=400, detail="Mailbox consent expired. Try again.")

    flow = _open_flow(sealed)
    result = client().acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    if "refresh_token" not in result:
        raise HTTPException(
            status_code=400,
            detail=result.get("error_description", "Mailbox consent was not granted."),
        )

    # Overwrite the identity-only token: consent is cumulative, so this one
    # covers both scope sets and the old one covers strictly less.
    await _store_refresh_token(user, result)

    payload = ConnectRequest.model_validate(flow["onboarding"])
    tenant_id = user.tenant_id
    mailbox_id = uuid.uuid4()
    folder_id = payload.folder_id or WELL_KNOWN_INBOX
    start_from = resolve_start_date(payload.start_from)

    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO mailboxes
                    (id, tenant_id, user_id, ms_user_id, scope, folder_id,
                     status, initial_sync_from, retention_months)
                VALUES (:id, :tenant, :user, :msid, :scope, :folder,
                        'active', :start, :retention)
                """
            ),
            {
                "id": mailbox_id,
                "tenant": tenant_id,
                "user": user.id,
                "msid": user.ms_object_id,
                "scope": payload.scope,
                "folder": folder_id,
                "start": start_from,
                "retention": settings.DEFAULT_RETENTION_MONTHS,
            },
        )

    client = await graph_client_for_mailbox(tenant_id, mailbox_id)
    try:
        await create_subscription(
            tenant_id, mailbox_id, user.ms_object_id, folder_id, client
        )
    finally:
        await client.aclose()

    await enqueue("backfill_mailbox_job", mailbox_id=str(mailbox_id))

    # The user arrives here from Entra's redirect, in a browser. Returning JSON
    # would leave them staring at a raw object; sign-in redirects for the same
    # reason.
    response = RedirectResponse(_frontend_url(f"/mailboxes/{mailbox_id}"), status_code=303)
    response.delete_cookie(_flow_cookie_name(state), path="/")
    return response
```

Add to `app/core/config.py`:

```python
    DEFAULT_RETENTION_MONTHS: int = 24
    # A separate redirect URI keeps the mailbox consent from landing on the
    # sign-in callback, which would create a session instead of a mailbox.
    MS_MAILBOX_REDIRECT_URI: str = ""
```

And `.env`:

```bash
DEFAULT_RETENTION_MONTHS=24
MS_MAILBOX_REDIRECT_URI=https://expressautomate.app/api/mailboxes/connect/callback
```

Register this second redirect URI in the Entra app registration — Entra rejects
any redirect it has not been told about, and the failure surfaces on the consent
screen rather than in your logs.

`_store_refresh_token(user, result)` is the same upsert `microsoft_callback`
already performs in `app/api/auth.py`; extract it there rather than writing a
second copy, so there is one place that encrypts a refresh token.

- [ ] **Step 5: Add the four jobs**

Two of these are enqueued by the lifecycle endpoint (Task 5) and by
`renew_subscriptions` (Task 9). Without them, arq logs an unknown-job error and
a mailbox with a revoked grant or a dropped subscription silently stops
ingesting — which looks exactly like a quiet week.

Append to `app/workers/jobs.py`:

```python
async def recreate_subscription(ctx, mailbox_id: str) -> None:
    """The subscription is gone or unrenewable; make a new one."""
    from app.services.graph.subscriptions import create_subscription

    tenant_id = await _tenant_of_mailbox(mailbox_id)
    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text("SELECT ms_user_id, folder_id FROM mailboxes WHERE id = :i"),
                {"i": mailbox_id},
            )
        ).one()
        # Retire the old record first: subscription_id is unique, and a stale
        # 'active' row would keep resolve_subscription pointed at a dead sub.
        await session.execute(
            text(
                "UPDATE graph_subscriptions SET status = 'replaced'"
                " WHERE mailbox_id = :i AND status = 'active'"
            ),
            {"i": mailbox_id},
        )

    client = await graph_client_for_mailbox(tenant_id, uuid.UUID(mailbox_id))
    try:
        await create_subscription(
            tenant_id, uuid.UUID(mailbox_id), row.ms_user_id, row.folder_id, client
        )
    finally:
        await client.aclose()
    # Notifications stopped while the subscription was dead; reconcile the gap.
    await enqueue("delta_sync_mailbox", mailbox_id=mailbox_id)


async def reauthorize_subscription(ctx, subscription_id: str) -> None:
    """Graph asked us to prove the grant is still good (plan §8).

    A successful token refresh is the proof. If it fails, the user has revoked
    access or let it lapse, and the honest response is to stop and tell them —
    not to retry a grant that no longer exists.
    """
    async with SessionLocal() as session:
        record = (
            await session.execute(
                text("SELECT * FROM resolve_subscription(:s)"), {"s": subscription_id}
            )
        ).one_or_none()
    if record is None:
        return

    try:
        await access_token_for_mailbox(record.tenant_id, record.mailbox_id)
    except PermissionError:
        await _mark_needs_reauth(record.tenant_id, record.mailbox_id)
        return

    client = await graph_client_for_mailbox(record.tenant_id, record.mailbox_id)
    try:
        from app.services.graph.subscriptions import renew_subscription

        await renew_subscription(record.tenant_id, subscription_id, client)
    finally:
        await client.aclose()


async def _mark_needs_reauth(tenant_id: uuid.UUID, mailbox_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("UPDATE mailboxes SET status = 'needs_reauth' WHERE id = :i"),
            {"i": mailbox_id},
        )
        # Leaving a dead subscription 'active' would make renew_subscriptions
        # retry it every fifteen minutes forever.
        await session.execute(
            text(
                "UPDATE graph_subscriptions SET status = 'revoked'"
                " WHERE mailbox_id = :i"
            ),
            {"i": mailbox_id},
        )
```

And the two onboarding jobs:

```python
async def backfill_mailbox_job(ctx, mailbox_id: str) -> None:
    """Initial historical walk (plan §6.2)."""
    from app.services.graph.delta import backfill_mailbox

    tenant_id, since = await _mailbox_backfill_start(mailbox_id)
    client = await graph_client_for_mailbox(tenant_id, uuid.UUID(mailbox_id))
    try:
        count = await backfill_mailbox(tenant_id, uuid.UUID(mailbox_id), client, since)
        log.info("backfill_complete", mailbox_id=mailbox_id, messages=count)
    finally:
        await client.aclose()


async def delta_sync_mailbox(ctx, mailbox_id: str) -> None:
    """Reconciliation walk for one mailbox (plan §9)."""
    from app.services.graph.delta import sync_mailbox

    tenant_id = await _tenant_of_mailbox(mailbox_id)
    client = await graph_client_for_mailbox(tenant_id, uuid.UUID(mailbox_id))
    try:
        await sync_mailbox(tenant_id, uuid.UUID(mailbox_id), client)
    finally:
        await client.aclose()


async def _tenant_of_mailbox(mailbox_id: str) -> uuid.UUID:
    async with SessionLocal() as session:
        return (
            await session.execute(
                text("SELECT tenant_id FROM active_mailboxes() WHERE mailbox_id = :i"),
                {"i": mailbox_id},
            )
        ).scalar_one()


async def _mailbox_backfill_start(mailbox_id: str) -> tuple[uuid.UUID, datetime]:
    tenant_id = await _tenant_of_mailbox(mailbox_id)
    async with tenant_session(tenant_id) as session:
        since = (
            await session.execute(
                text("SELECT initial_sync_from FROM mailboxes WHERE id = :i"),
                {"i": mailbox_id},
            )
        ).scalar_one()
    return tenant_id, since
```

Register all four in `app/workers/settings.py` (never in `queue.py` — see Task 6):

```python
from app.workers.jobs import (
    backfill_mailbox_job,
    delta_sync_mailbox,
    fetch_email,
    reauthorize_subscription,
    recreate_subscription,
)


class WorkerSettings:
    functions = [
        fetch_email,
        backfill_mailbox_job,
        delta_sync_mailbox,
        recreate_subscription,
        reauthorize_subscription,
    ]
```

- [ ] **Step 6: Mount the router**

In `app/main.py`:

```python
from app.api import mailboxes

app.include_router(mailboxes.router, prefix="/api")
```

- [ ] **Step 6: Run the whole suite**

```bash
uv run pytest -v
uv run ruff check .
```

Expected: PASS, no lint errors. `tests/test_routing.py` confirms every new route lives under `/api`.

- [ ] **Step 7: Commit**

```bash
git add app/api/mailboxes.py app/workers app/main.py app/core/config.py tests/test_mailbox_onboarding.py
git commit -m "Connect a mailbox, subscribe, then backfill"
```

---

### Task 11: Deployment wiring

**Files:**
- Modify: `Dockerfile`, `.github/workflows/` (worker service), `docs/setup.md`
- Test: `tests/test_deploy_payload.py` (extend)

**Interfaces:**
- Consumes: everything above
- Produces: a running arq worker process alongside the API and supervisor

- [ ] **Step 1: Write the failing test**

Extend `tests/test_deploy_payload.py`:

```python
def test_every_new_ingestion_setting_has_an_env_entry():
    """A setting with no .env entry boots with a silent default in production."""
    from pathlib import Path

    from app.core.config import Settings

    env_text = (Path(__file__).resolve().parents[2] / ".env").read_text()
    required = [
        "GRAPH_BASE_URL",
        "GRAPH_DEFAULT_RETRY_AFTER_SECONDS",
        "GRAPH_SUBSCRIPTION_REQUEST_MINUTES",
        "GRAPH_SUBSCRIPTION_RENEW_MARGIN",
        "MS_WEBHOOK_LIFECYCLE_URL",
        "MS_IDENTITY_SCOPES",
        "MS_MAILBOX_SCOPES",
        "MS_MAILBOX_REDIRECT_URI",
        "ARQ_POLL_DELAY_SECONDS",
        "ARQ_MAX_JOBS",
        "ARQ_MAX_TRIES",
        "INITIAL_SYNC_MAX_MESSAGES",
        "INITIAL_SYNC_MAX_LOOKBACK_DAYS",
        "RESCAN_PENDING_MINUTES",
        "RESCAN_WORKING_MINUTES",
        "DEFAULT_RETENTION_MONTHS",
    ]
    missing = [name for name in required if f"{name}=" not in env_text]
    assert missing == [], f"settings with no .env entry: {missing}"

    declared = set(Settings.model_fields)
    assert set(required) <= declared
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deploy_payload.py -v`
Expected: FAIL, listing any setting missing from `.env`.

- [ ] **Step 3: Complete the `.env` entries**

Add every name the test lists, using the values given in earlier tasks.

- [ ] **Step 4: Add the arq worker to the container**

The image must be able to start three distinct processes. In `Dockerfile`, keep
the existing API entrypoint and document the alternates:

```dockerfile
# One image, three entrypoints — Koyeb picks per service:
#   api        : uvicorn app.main:app --host 0.0.0.0 --port 8000
#   supervisor : python -m app.workers.main
#   arq        : arq app.workers.settings.WorkerSettings
```

- [ ] **Step 5: Document the two Koyeb settings that are not in this repo**

In `docs/setup.md`, extend the existing Koyeb section:

```markdown
### Ingestion services

| Service | Command | Notes |
|---|---|---|
| `api` | `uvicorn app.main:app` | Route `/`, health check `/api/health` |
| `worker` | `python -m app.workers.main` | Periodic recovery. No health check |
| `arq` | `arq app.workers.settings.WorkerSettings` | Job processing. No health check |

`MS_WEBHOOK_NOTIFICATION_URL` and `MS_WEBHOOK_LIFECYCLE_URL` must be publicly
reachable before a subscription can be created — Graph validates the endpoint
synchronously at creation and refuses if the handshake fails.
```

- [ ] **Step 6: Run the whole suite**

```bash
uv run pytest -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add .env.example Dockerfile docs/setup.md tests/test_deploy_payload.py
git commit -m "Run the ingestion worker alongside the API"
```

---

## Self-Review

**Spec coverage.** Every section of the ingestion half of the spec maps to a task:
routing tables and RLS → Task 1; Graph client and throttling → Task 2; R2 and
deterministic keys → Task 3; queue and the soft-fail enqueue → Task 4; webhook,
validation handshake, `clientState`, lifecycle events → Task 5; fetch, R2-before-status,
`unfetchable` → Task 6; subscription create and renewal on granted lifetime → Task 7;
delta sync, `source_state`, backfill cap → Task 8; `rescan_stuck` across every
non-terminal status, renewal sweep, delta sweep → Task 9; onboarding order and
lookback clamp → Task 10; configuration and deployment → Task 11.

**Not covered here, by design:** classification, extraction, evidence validation,
`opportunities`, and retention purging. Those are the follow-on plan. The
`classify_email` job this plan enqueues does not exist until then — until it does,
rows will accumulate at `fetched`, which `rescan_stuck` will retry harmlessly.
Task 1 of the follow-on plan must land before this pipeline is switched on in
production.

**Type consistency.** `email_message_id` is a string in every job signature.
`tenant_id` and `mailbox_id` are `uuid.UUID` in every service signature and
strings only at arq boundaries, because arq serialises job arguments as JSON.
`body_key(tenant_id, mailbox_id, message_id, kind)` has the same four positional
parameters everywhere. `graph_client_for_mailbox(tenant_id, mailbox_id)` takes
both, in that order, in Tasks 6, 9, and 10.
