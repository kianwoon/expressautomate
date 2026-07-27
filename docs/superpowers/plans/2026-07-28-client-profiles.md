# Client Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each agency a list of the client companies it recruits for, proposed automatically from the sender domain of ingested email and confirmed by a human.

**Architecture:** Two new tenant-scoped tables (`clients`, `client_mentions`) behind Postgres row-level security. A matcher service runs inside the existing extraction transaction in `persist()`, resolving sender domain → normalized name → new proposal. A read/write API mirrors `app/api/opportunities.py`. Merge, confirm, archive and unmerge are human-only transitions.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Alembic, Postgres 16, pytest-asyncio, `uv`.

Spec: [2026-07-28-tenant-profiles-design.md](../specs/2026-07-28-tenant-profiles-design.md)

## Global Constraints

- All commands run from `backend/`. Prefix every Python command with `uv run`.
- **No hardcoded values in source.** Every tunable is a field on `app.core.config.settings` (`app/core/config.py`). A literal provider list, page size, or URL in a module is a defect.
- **Every tenant-scoped table must ENABLE + FORCE row level security and carry the `tenant_isolation` policy in its own migration.** `verify_rls_enforced()` (`app/db/rls.py:58`) discovers tables by catalog query and refuses to boot the app on any readable table missing FORCE. A migration that creates a table without the policy breaks startup, not just a test.
- **All reads and writes go through `tenant_session(tenant_uuid)`** (`app/db/rls.py:35`). A plain `SessionLocal()` is one edit away from a cross-agency leak.
- **Never fabricate a missing value** (plan §15). Absent data stays NULL; it does not become `""`, `0`, or a guess.
- Tests run against a local throwaway Postgres. `tests/conftest.py:42` aborts collection if `DATABASE_URL` or `DATABASE_ADMIN_URL` points at a non-local host.
- Every route lives under `/api`. `tests/test_routing.py` fails if one escapes, because the static frontend mount shadows it.
- Run `uv run ruff check .` before each commit.

---

### Task 1: Configuration

Two settings the later tasks depend on. Doing this first means no task is tempted to inline a literal.

**Files:**
- Modify: `backend/app/core/config.py`
- Test: `backend/tests/test_client_config.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `settings.FREE_EMAIL_DOMAINS: frozenset[str]` (lowercased email domains that must never key a client) and `settings.CLIENTS_PAGE_LIMIT: int`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_client_config.py`:

```python
"""The free-domain set is configuration, not a literal in the matcher.

A recruiter's own agency is on a real domain; their candidates and some
clients are on gmail.com. Keying a client on a free provider would collapse
every unrelated company into one row, so the set has to exist before the
matcher does — and it has to be a setting, because which providers count is
an operator's judgement and changes without a deploy.
"""

from app.core.config import settings


def test_free_email_domains_covers_the_common_providers() -> None:
    for provider in ("gmail.com", "hotmail.com", "outlook.com", "yahoo.com"):
        assert provider in settings.FREE_EMAIL_DOMAINS


def test_free_email_domains_is_lowercased_and_hashable() -> None:
    assert all(d == d.lower() for d in settings.FREE_EMAIL_DOMAINS)
    assert isinstance(settings.FREE_EMAIL_DOMAINS, frozenset)


def test_clients_page_limit_is_a_positive_int() -> None:
    assert isinstance(settings.CLIENTS_PAGE_LIMIT, int)
    assert settings.CLIENTS_PAGE_LIMIT > 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_client_config.py -v
```

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'FREE_EMAIL_DOMAINS'`.

- [ ] **Step 3: Add the settings**

In `backend/app/core/config.py`, next to `OPPORTUNITIES_PAGE_LIMIT` (line 290), add:

```python
    CLIENTS_PAGE_LIMIT: int = Field(default=200, gt=0)

    # Which domains may never key a client. A hiring manager writing from
    # gmail.com identifies a person, not a company, and matching on that
    # domain would file every unrelated agency's clients under one row.
    # Stored as a comma-separated string in the environment and split here so
    # the operator can extend it without a code change.
    FREE_EMAIL_DOMAINS_RAW: str = Field(
        default="gmail.com,googlemail.com,hotmail.com,outlook.com,live.com,"
        "yahoo.com,yahoo.com.sg,icloud.com,me.com,proton.me,protonmail.com,"
        "aol.com,qq.com,163.com",
        alias="FREE_EMAIL_DOMAINS",
    )

    @property
    def FREE_EMAIL_DOMAINS(self) -> frozenset[str]:
        """Parsed once per access; the raw string is what the environment sets."""
        return frozenset(
            part.strip().lower()
            for part in self.FREE_EMAIL_DOMAINS_RAW.split(",")
            if part.strip()
        )
```

If the `Settings` class sets `populate_by_name=False` or lacks alias support, drop the `alias=` argument and name the env var `FREE_EMAIL_DOMAINS_RAW`. Check the existing `model_config` before choosing.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_client_config.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py tests/test_client_config.py
git commit -m "Make the free-provider list an operator's setting, not a literal"
```

---

### Task 2: Tables, RLS, and the cross-tenant foreign key

The isolation task. The FK test is written first and must genuinely fail, because RLS does not filter foreign-key validation — a mention in agency A can reference agency B's client unless the FK is composite.

**Files:**
- Create: `backend/app/models/client.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/20260728_1100_client_profiles.py`
- Test: `backend/tests/test_client_isolation.py` (create)

**Interfaces:**
- Consumes: `TenantScoped`, `UUIDPrimaryKey`, `Timestamps` from `app.db.base`.
- Produces: `app.models.Client` (table `clients`) and `app.models.ClientMention` (table `client_mentions`); the status constants `Client.UNCONFIRMED`, `CONFIRMED`, `MERGED`, `ARCHIVED`.

- [ ] **Step 1: Write the failing isolation test**

Create `backend/tests/test_client_isolation.py`:

```python
"""Agency A must never reach agency B's clients — including by foreign key.

The FK case is the one RLS does not cover. A policy filters what a statement
can SELECT and what it may INSERT, but PostgreSQL validates a foreign key
with an internal referential-integrity check that is not subject to the
policy. So a mention row in agency A can name agency B's client_id and the
database will happily accept it, silently stitching one agency's evidence
onto another's record. Only a composite FK carrying tenant_id closes it.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session


async def _seed_tenant(tenant_id: uuid.UUID, slug: str) -> None:
    from tests.conftest import AdminSessionLocal

    async with AdminSessionLocal() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
            {"i": tenant_id, "n": slug, "s": slug},
        )
        await session.commit()


@pytest.fixture
async def two_agencies():
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(a, f"agency-a-{a.hex[:6]}")
    await _seed_tenant(b, f"agency-b-{b.hex[:6]}")
    yield a, b
    from tests.conftest import AdminSessionLocal

    async with AdminSessionLocal() as session:
        for tid in (a, b):
            await session.execute(
                text("DELETE FROM client_mentions WHERE tenant_id = :t"), {"t": tid}
            )
            await session.execute(text("DELETE FROM clients WHERE tenant_id = :t"), {"t": tid})
            await session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await session.commit()


async def test_one_agency_cannot_read_anothers_clients(two_agencies) -> None:
    a, b = two_agencies
    async with tenant_session(a) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                "VALUES (:i, :t, 'Acme', 'acme', 'unconfirmed')"
            ),
            {"i": uuid.uuid4(), "t": a},
        )
        await session.commit()

    async with tenant_session(b) as session:
        rows = (await session.execute(text("SELECT id FROM clients"))).all()
    assert rows == []


async def test_a_mention_cannot_reference_another_agencys_client(two_agencies) -> None:
    a, b = two_agencies
    client_id = uuid.uuid4()
    async with tenant_session(a) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status) "
                "VALUES (:i, :t, 'Acme', 'acme', 'unconfirmed')"
            ),
            {"i": client_id, "t": a},
        )
        await session.commit()

    # Agency B names agency A's client. The composite FK must reject it.
    with pytest.raises(IntegrityError):
        async with tenant_session(b) as session:
            await session.execute(
                text(
                    "INSERT INTO client_mentions "
                    "(id, tenant_id, client_id, matched_by) "
                    "VALUES (:i, :t, :c, 'human')"
                ),
                {"i": uuid.uuid4(), "t": b, "c": client_id},
            )
            await session.commit()
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
uv run pytest tests/test_client_isolation.py -v
```

Expected: FAIL — `UndefinedTableError: relation "clients" does not exist`.

- [ ] **Step 3: Write the models**

Create `backend/app/models/client.py`:

```python
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

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Numeric, String, Text, UniqueConstraint
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
```

- [ ] **Step 4: Register the models**

In `backend/app/models/__init__.py`, add the import after line 1 and the names to `__all__` in alphabetical position:

```python
from app.models.client import Client, ClientMention
```

```python
    "Client",
    "ClientMention",
```

- [ ] **Step 5: Write the migration**

Create `backend/alembic/versions/20260728_1100_client_profiles.py`. `down_revision` is `d4a81c7f6b30` (the `opportunity_codes` migration — confirm with `uv run alembic heads` before writing).

```python
"""client profiles

Revision ID: e5b92d8a7c41
Revises: d4a81c7f6b30
Create Date: 2026-07-28 11:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e5b92d8a7c41'
down_revision: str | None = 'd4a81c7f6b30'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED: list[tuple[str, str]] = [
    ("clients", "tenant_id"),
    ("client_mentions", "tenant_id"),
]

SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        'clients',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('name_normalized', sa.Text(), nullable=False),
        sa.Column('email_domain', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='unconfirmed'),
        sa.Column('merged_into_client_id', sa.UUID(), nullable=True),
        sa.Column('first_seen_email_message_id', sa.UUID(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['first_seen_email_message_id'], ['email_messages.id'], ondelete='SET NULL'
        ),
        # The target of a child's composite foreign key.
        sa.UniqueConstraint('tenant_id', 'id', name='uq_clients_tenant_id_id'),
        # A merged row that does not say what it merged into is unusable, and a
        # live row pointing somewhere else is a contradiction. Both directions
        # are enforced because only checking one leaves the other reachable.
        sa.CheckConstraint(
            "(status = 'merged') = (merged_into_client_id IS NOT NULL)",
            name='ck_clients_merged_has_target',
        ),
        sa.CheckConstraint(
            "status IN ('unconfirmed', 'confirmed', 'merged', 'archived')",
            name='ck_clients_status',
        ),
    )
    op.create_index(op.f('ix_clients_tenant_id'), 'clients', ['tenant_id'])
    op.create_index(op.f('ix_clients_name_normalized'), 'clients', ['name_normalized'])
    op.create_index(op.f('ix_clients_status'), 'clients', ['status'])
    op.create_index(op.f('ix_clients_last_seen_at'), 'clients', ['last_seen_at'])

    # Self-FK added after the table exists, and composite so a merge target can
    # never be another agency's client.
    op.create_foreign_key(
        'fk_clients_merged_into_same_tenant',
        'clients',
        'clients',
        ['tenant_id', 'merged_into_client_id'],
        ['tenant_id', 'id'],
        ondelete='SET NULL',
    )

    # The domain key. `merged` is excluded so a merge frees the domain for the
    # surviving row; `archived` is deliberately INCLUDED, because the matcher
    # matches archived clients and an excluded archived row would send it to
    # the insert path and straight into a unique violation.
    op.create_index(
        'uq_clients_tenant_domain',
        'clients',
        ['tenant_id', 'email_domain'],
        unique=True,
        postgresql_where=sa.text("email_domain IS NOT NULL AND status <> 'merged'"),
    )

    op.create_table(
        'client_mentions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('client_id', sa.UUID(), nullable=False),
        sa.Column('email_message_id', sa.UUID(), nullable=True),
        sa.Column('matched_by', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.Numeric(4, 3), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'client_id'],
            ['clients.tenant_id', 'clients.id'],
            name='fk_client_mentions_client_same_tenant',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['email_message_id'], ['email_messages.id'], ondelete='SET NULL'
        ),
        sa.UniqueConstraint(
            'tenant_id', 'client_id', 'email_message_id',
            name='uq_client_mentions_once_per_message',
        ),
        sa.CheckConstraint(
            "matched_by IN ('email_domain', 'name', 'human')",
            name='ck_client_mentions_matched_by',
        ),
    )
    op.create_index(op.f('ix_client_mentions_tenant_id'), 'client_mentions', ['tenant_id'])
    op.create_index(op.f('ix_client_mentions_client_id'), 'client_mentions', ['client_id'])
    op.create_index(
        op.f('ix_client_mentions_email_message_id'), 'client_mentions', ['email_message_id']
    )

    _enforce_rls()
    _touch_updated_at()


def _touch_updated_at() -> None:
    """Bind the existing trigger, so `updated_at` means the same thing here."""
    for table, _column in PROTECTED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_touch_updated_at ON {table}")
        op.execute(
            f"""
            CREATE TRIGGER {table}_touch_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
            """
        )


def _enforce_rls() -> None:
    """The same policy every tenant-scoped table carries, for the same reasons.

    FORCE, not merely ENABLE: without it the table owner bypasses the policy,
    and the owner is who migrations and any superuser session connect as.
    """
    for table, column in PROTECTED:
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


def downgrade() -> None:
    op.drop_table('client_mentions')
    op.drop_index('uq_clients_tenant_domain', table_name='clients')
    op.drop_table('clients')
```

- [ ] **Step 6: Apply and run the test**

```bash
uv run alembic upgrade head && uv run pytest tests/test_client_isolation.py -v
```

Expected: 2 passed. If `test_a_mention_cannot_reference_another_agencys_client` passes only because the insert failed for some *other* reason, check the error text names `fk_client_mentions_client_same_tenant`.

- [ ] **Step 7: Confirm the app still boots**

```bash
uv run pytest tests/test_guards.py -v
```

Expected: PASS. This is the suite that exercises `verify_rls_enforced()`; a table missing FORCE fails here rather than in production.

- [ ] **Step 8: Commit**

```bash
git add app/models/client.py app/models/__init__.py alembic/versions/20260728_1100_client_profiles.py tests/test_client_isolation.py
git commit -m "Give clients a table that one agency cannot reach from another"
```

---

### Task 3: Name normalization

A small pure function, isolated so the matcher's tests do not have to relitigate string handling.

**Files:**
- Create: `backend/app/services/client_naming.py`
- Test: `backend/tests/test_client_naming.py` (create)

**Interfaces:**
- Consumes: `settings.FREE_EMAIL_DOMAINS` from Task 1.
- Produces: `normalize_company_name(raw: str) -> str` and `domain_of(email: str | None) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_client_naming.py`:

```python
"""Normalisation decides what the matcher will *propose*, never what it accepts.

Which is why it stays blunt. A cleverer normaliser that folded "Acme
Engineering" into "Acme" would generate confident-looking proposals across
unrelated companies, and a recruiter clicking through a review queue has no
way to tell a good proposal from a plausible one.
"""

import pytest

from app.services.client_naming import domain_of, normalize_company_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Pte Ltd", "acme"),
        ("ACME PTE. LTD.", "acme"),
        ("  Acme   Holdings  ", "acme holdings"),
        ("Acme Pte Ltd.", "acme"),
        ("Acme Private Limited", "acme"),
        ("Acme, Inc.", "acme"),
        ("Acme LLC", "acme"),
    ],
)
def test_strips_legal_suffixes_and_collapses_space(raw: str, expected: str) -> None:
    assert normalize_company_name(raw) == expected


def test_a_name_that_is_only_a_suffix_survives() -> None:
    # Stripping to empty would make every such row collide with every other.
    assert normalize_company_name("Ltd") == "ltd"


def test_empty_input_normalizes_to_empty() -> None:
    assert normalize_company_name("   ") == ""


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("jane@Acme.com.SG", "acme.com.sg"),
        ("jane@acme.com", "acme.com"),
        ("jane@gmail.com", None),          # free provider: identifies a person
        ("JANE@GMAIL.COM", None),
        (None, None),                       # sender_email is nullable
        ("not-an-email", None),
        ("", None),
    ],
)
def test_domain_of_rejects_free_providers_and_junk(email: str | None, expected: str | None) -> None:
    assert domain_of(email) == expected
```

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/test_client_naming.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.client_naming'`.

- [ ] **Step 3: Implement**

Create `backend/app/services/client_naming.py`:

```python
"""Turning what an email says into something two rows can be compared on.

Both functions are total and pure: they return a value or None for every
input, including the nulls the pipeline genuinely produces. `sender_email` is
nullable on `email_messages`, and a matcher that raised on a null sender would
fail an ingest run over a message that is merely unusual.
"""

import re

from app.core.config import settings

# Order matters: the longer forms are tried first, so "Pte Ltd" is not left as
# a dangling "Pte" by an earlier match on "Ltd".
_LEGAL_SUFFIXES = (
    "private limited",
    "pte ltd",
    "pte",
    "sdn bhd",
    "limited",
    "ltd",
    "llc",
    "llp",
    "inc",
    "corp",
    "corporation",
    "co",
    "gmbh",
    "bv",
    "nv",
    "sa",
    "ag",
)

_PUNCTUATION = re.compile(r"[.,]")
_WHITESPACE = re.compile(r"\s+")


def normalize_company_name(raw: str) -> str:
    """Lowercase, drop punctuation and trailing legal suffixes, collapse space.

    Deliberately conservative. This value only ever *proposes* a match to a
    human, so a false negative costs one extra click and a false positive
    costs a recruiter's trust in the review queue.
    """
    text = _PUNCTUATION.sub(" ", raw.lower())
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""

    # Peel suffixes from the end until none matches, so "Acme Pte Ltd" and
    # "Acme Ltd" agree. A name that is *only* a suffix keeps it — normalising
    # "Ltd" to "" would make it collide with every other empty result.
    changed = True
    while changed:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -len(suffix) - 1].strip()
                changed = True
                break
    return text


def domain_of(email: str | None) -> str | None:
    """The mail domain, unless it identifies a person rather than a company.

    Returns None for a null or malformed address and for every provider in
    `settings.FREE_EMAIL_DOMAINS`. None means "no domain key available", which
    sends the matcher to name matching rather than inventing one (§15).
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain or domain in settings.FREE_EMAIL_DOMAINS:
        return None
    return domain
```

- [ ] **Step 4: Run it**

```bash
uv run pytest tests/test_client_naming.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/client_naming.py tests/test_client_naming.py
git commit -m "Compare company names on something blunt enough to trust"
```

---

### Task 4: The matcher

**Files:**
- Create: `backend/app/services/client_matching.py`
- Test: `backend/tests/test_client_matching.py` (create)

**Interfaces:**
- Consumes: `normalize_company_name`, `domain_of` (Task 3); `Client`, `ClientMention` (Task 2).
- Produces: `async def match_client(session, tenant_id, email_message_id, sender_email, company_name) -> uuid.UUID | None` — resolves or creates a client, records exactly one mention, and returns the surviving client id. Returns `None` when there is nothing to match on. **Takes an existing session**; it does not open its own, because it runs inside `persist()`'s transaction.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_client_matching.py`:

```python
"""What the matcher may decide on its own, and what it must leave to a person.

The matcher may: link a message to a client whose domain it already knows,
and create a new unconfirmed proposal. It may not: confirm anything, merge
anything, or un-archive anything. Every test here is about that boundary —
the storage is the easy part.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.client_matching import match_client
from tests.conftest import AdminSessionLocal


@pytest.fixture
async def agency():
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    yield tid
    async with AdminSessionLocal() as s:
        await s.execute(text("DELETE FROM client_mentions WHERE tenant_id = :t"), {"t": tid})
        await s.execute(
            text("UPDATE clients SET merged_into_client_id = NULL WHERE tenant_id = :t"), {"t": tid}
        )
        await s.execute(text("DELETE FROM clients WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _status_of(tenant_id: uuid.UUID, client_id: uuid.UUID) -> str:
    async with tenant_session(tenant_id) as s:
        return (
            await s.execute(text("SELECT status FROM clients WHERE id = :i"), {"i": client_id})
        ).scalar_one()


async def _mention_count(tenant_id: uuid.UUID, client_id: uuid.UUID) -> int:
    async with tenant_session(tenant_id) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM client_mentions WHERE client_id = :i"), {"i": client_id}
            )
        ).scalar_one()


async def test_an_unknown_domain_becomes_an_unconfirmed_proposal(agency) -> None:
    async with tenant_session(agency) as s:
        cid = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    assert cid is not None
    assert await _status_of(agency, cid) == "unconfirmed"


async def test_the_same_domain_twice_is_one_client(agency) -> None:
    async with tenant_session(agency) as s:
        first = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        second = await match_client(s, agency, None, "jobs@acme.com.sg", "ACME")
        await s.commit()
    assert first == second


async def test_a_free_provider_never_keys_a_client(agency) -> None:
    async with tenant_session(agency) as s:
        a = await match_client(s, agency, None, "alice@gmail.com", "Acme Pte Ltd")
        b = await match_client(s, agency, None, "bob@gmail.com", "Globex Ltd")
        await s.commit()
    assert a != b
    async with tenant_session(agency) as s:
        domains = (await s.execute(text("SELECT email_domain FROM clients"))).scalars().all()
    assert domains == [None, None]


async def test_a_name_match_attaches_but_does_not_confirm(agency) -> None:
    async with tenant_session(agency) as s:
        first = await match_client(s, agency, None, "alice@gmail.com", "Acme Pte Ltd")
        second = await match_client(s, agency, None, "bob@gmail.com", "ACME PTE. LTD.")
        await s.commit()
    assert first == second
    assert await _status_of(agency, first) == "unconfirmed"


async def test_a_null_sender_falls_through_to_the_name(agency) -> None:
    async with tenant_session(agency) as s:
        cid = await match_client(s, agency, None, None, "Acme Pte Ltd")
        await s.commit()
    assert cid is not None
    async with tenant_session(agency) as s:
        domain = (
            await s.execute(text("SELECT email_domain FROM clients WHERE id = :i"), {"i": cid})
        ).scalar_one()
    assert domain is None


async def test_nothing_to_match_on_produces_nothing(agency) -> None:
    async with tenant_session(agency) as s:
        assert await match_client(s, agency, None, None, None) is None
        assert await match_client(s, agency, None, None, "   ") is None
        await s.commit()


async def test_reprocessing_the_same_message_adds_no_second_mention(agency) -> None:
    message_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, subject) "
                "VALUES (:i, :t, 'x') ON CONFLICT DO NOTHING"
            ),
            {"i": message_id, "t": agency},
        )
        await s.commit()

    async with tenant_session(agency) as s:
        cid = await match_client(s, agency, message_id, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    async with tenant_session(agency) as s:
        again = await match_client(s, agency, message_id, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()

    assert cid == again
    assert await _mention_count(agency, cid) == 1


async def test_re_seeing_an_archived_client_does_not_resurrect_it(agency) -> None:
    async with tenant_session(agency) as s:
        cid = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    async with tenant_session(agency) as s:
        await s.execute(
            text("UPDATE clients SET status = 'archived' WHERE id = :i"), {"i": cid}
        )
        await s.commit()

    async with tenant_session(agency) as s:
        again = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()

    # Same row — the archived client still holds the domain index slot, so an
    # insert here would be a unique violation, not a new client.
    assert again == cid
    assert await _status_of(agency, cid) == "archived"


async def test_re_seeing_a_merged_client_lands_on_the_survivor(agency) -> None:
    async with tenant_session(agency) as s:
        loser = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        winner = await match_client(s, agency, None, "hr@acme-group.com", "Acme Group")
        await s.commit()
    async with tenant_session(agency) as s:
        await s.execute(
            text(
                "UPDATE clients SET status = 'merged', merged_into_client_id = :w WHERE id = :l"
            ),
            {"w": winner, "l": loser},
        )
        await s.commit()

    async with tenant_session(agency) as s:
        landed = await match_client(s, agency, None, "hr@acme.com.sg", "Acme Pte Ltd")
        await s.commit()
    assert landed == winner
```

- [ ] **Step 2: Run them**

```bash
uv run pytest tests/test_client_matching.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.client_matching'`.

- [ ] **Step 3: Implement**

Create `backend/app/services/client_matching.py`:

```python
"""Deciding which client an email is about.

Three steps, first hit wins: the sender's domain, then the normalised company
name, then a new proposal. Only the first is an identity claim the pipeline is
entitled to make on its own — a domain is a fact about where the mail came
from. A name match records that the two look alike and leaves the row
unconfirmed for a person to judge.

The service takes a session rather than opening one. It runs inside the
extraction transaction in `persist()`, and a second connection would let the
extraction roll back while the client it proposed survived.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.services.client_naming import domain_of, normalize_company_name

_BY_DOMAIN = text(
    """
    SELECT id, status, merged_into_client_id FROM clients
    WHERE email_domain = :domain
    LIMIT 1
    """
)

# Name matching ignores merged rows — a merged row's identity now belongs to
# its target — and prefers the most recently seen of any remaining ties.
_BY_NAME = text(
    """
    SELECT id, status, merged_into_client_id FROM clients
    WHERE name_normalized = :name AND status <> 'merged'
    ORDER BY last_seen_at DESC NULLS LAST, created_at DESC
    LIMIT 1
    """
)

_INSERT_CLIENT = text(
    """
    INSERT INTO clients
        (id, tenant_id, name, name_normalized, email_domain, status,
         first_seen_email_message_id, last_seen_at)
    VALUES (:id, :tenant_id, :name, :name_normalized, :domain, 'unconfirmed',
            :message_id, now())
    ON CONFLICT (tenant_id, email_domain)
        WHERE email_domain IS NOT NULL AND status <> 'merged'
    DO UPDATE SET last_seen_at = now()
    RETURNING id
    """
)

_TOUCH = text("UPDATE clients SET last_seen_at = now() WHERE id = :id")

# The unique constraint makes a repeated mention a no-op. `DO NOTHING` rather
# than an existence check, because two workers can reach this line at once.
_INSERT_MENTION = text(
    """
    INSERT INTO client_mentions (id, tenant_id, client_id, email_message_id, matched_by)
    VALUES (:id, :tenant_id, :client_id, :message_id, :matched_by)
    ON CONFLICT (tenant_id, client_id, email_message_id) DO NOTHING
    """
)


async def match_client(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID | None,
    sender_email: str | None,
    company_name: str | None,
) -> uuid.UUID | None:
    """Resolve this email to a client, recording how. Returns the client id.

    Returns None when the email offers neither a usable domain nor a company
    name. That is a real outcome, not an error: a message can legitimately
    mention no company, and inventing a client for it would be exactly the
    fabrication the pipeline exists to avoid (§15).
    """
    domain = domain_of(sender_email)
    normalized = normalize_company_name(company_name) if company_name else ""

    if domain is None and not normalized:
        return None

    client_id, matched_by = await _resolve(session, tenant_id, domain, normalized, company_name,
                                           email_message_id)
    if client_id is None:
        return None

    await session.execute(
        _INSERT_MENTION,
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "client_id": client_id,
            "message_id": email_message_id,
            "matched_by": matched_by,
        },
    )
    return client_id


async def _resolve(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    domain: str | None,
    normalized: str,
    company_name: str | None,
    email_message_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, str]:
    if domain is not None:
        row = (await session.execute(_BY_DOMAIN, {"domain": domain})).first()
        if row is not None:
            return await _surviving(session, row), "email_domain"

    if normalized:
        row = (await session.execute(_BY_NAME, {"name": normalized})).first()
        if row is not None:
            return await _surviving(session, row), "name"

    if not normalized:
        # A domain with no name still deserves a row; the domain is the name
        # we have, and labelling it anything else would be a guess.
        normalized = domain or ""

    new_id = (
        await session.execute(
            _INSERT_CLIENT,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "name": (company_name or domain or "").strip(),
                "name_normalized": normalized,
                "domain": domain,
                "message_id": email_message_id,
            },
        )
    ).scalar_one()
    return new_id, "email_domain" if domain else "name"


async def _surviving(session: AsyncSession, row) -> uuid.UUID:
    """The row a match should attach to, following one merge hop.

    A match never changes status. Re-seeing an archived client records that it
    was seen and leaves it archived — un-archiving is a judgement about whether
    the agency still works with that company, which is a person's to make.
    """
    client_id = row.merged_into_client_id if row.status == Client.MERGED else row.id
    await session.execute(_TOUCH, {"id": client_id})
    return client_id
```

Note on `ON CONFLICT` with a partial index: Postgres requires the conflict target to name the same predicate as the index. If asyncpg rejects the inline `WHERE`, name the constraint instead by creating the index as a named unique index (it already is: `uq_clients_tenant_domain`) and use `ON CONFLICT ON CONSTRAINT` only if the index was created as a constraint — a partial index cannot be, so keep the predicate form and verify against a live database in Step 4.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_client_matching.py -v
```

Expected: all passed. If `test_the_same_domain_twice_is_one_client` fails on the `ON CONFLICT` clause, the predicate does not match the index definition — compare it against the `postgresql_where` in the migration character by character.

- [ ] **Step 5: Commit**

```bash
git add app/services/client_matching.py tests/test_client_matching.py
git commit -m "Let the pipeline propose a client, and only a person confirm one"
```

---

### Task 5: Wire the matcher into ingestion

**Files:**
- Modify: `backend/app/services/ingest/persist.py:116-169`
- Test: `backend/tests/test_client_ingestion.py` (create)

**Interfaces:**
- Consumes: `match_client` (Task 4).
- Produces: no new API; `persist()` keeps its existing signature and return type (`list[uuid.UUID]` of opportunity ids).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_client_ingestion.py`:

```python
"""Extraction and client proposal are one transaction, or neither happened.

`persist()` already commits the extraction, its vacancies, its evidence and
its glossary codes together, on the grounds that a partial write is
indistinguishable from a complete one downstream. The client belongs in the
same transaction for the same reason: a client proposed by an extraction that
rolled back is a row nothing in the system can explain.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from tests.conftest import AdminSessionLocal


@pytest.fixture
async def agency():
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    yield tid
    async with AdminSessionLocal() as s:
        for table in ("client_mentions", "clients", "email_messages"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def test_persisting_an_extraction_proposes_the_sender_as_a_client(agency) -> None:
    """Build the ExtractionResponse and LLMResult exactly as
    tests/test_extract_job.py does — copy its factory helpers rather than
    inventing new ones, so this test breaks when the real contract changes.
    """
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, subject, sender_email) "
                "VALUES (:i, :t, 'Vacancy', 'hr@acme.com.sg')"
            ),
            {"i": message_id, "t": agency},
        )
        await s.commit()

    response, result = _extraction_fixture(company_name="Acme Pte Ltd")
    await persist(agency, message_id, response, result, source="Vacancy at Acme Pte Ltd")

    async with tenant_session(agency) as s:
        rows = (
            await s.execute(text("SELECT email_domain, status FROM clients"))
        ).all()
    assert rows == [("acme.com.sg", "unconfirmed")]


async def test_running_persist_twice_leaves_one_client_and_one_mention(agency) -> None:
    from app.services.ingest.persist import persist

    message_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO email_messages (id, tenant_id, subject, sender_email) "
                "VALUES (:i, :t, 'Vacancy', 'hr@acme.com.sg')"
            ),
            {"i": message_id, "t": agency},
        )
        await s.commit()

    response, result = _extraction_fixture(company_name="Acme Pte Ltd")
    await persist(agency, message_id, response, result, source="x")
    await persist(agency, message_id, response, result, source="x")

    async with tenant_session(agency) as s:
        clients = (await s.execute(text("SELECT count(*) FROM clients"))).scalar_one()
        mentions = (await s.execute(text("SELECT count(*) FROM client_mentions"))).scalar_one()
    assert (clients, mentions) == (1, 1)
```

`_extraction_fixture` must be copied from the existing helpers in `tests/test_extract_job.py`. Read that file and reuse its construction of `ExtractionResponse` and `LLMResult`; do not invent a parallel fixture, because a second definition of the extraction contract will drift from the first.

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/test_client_ingestion.py -v
```

Expected: FAIL — the `clients` table is empty; `rows == []`.

- [ ] **Step 3: Call the matcher from `persist()`**

In `backend/app/services/ingest/persist.py`, add the import at the top:

```python
from app.services.client_matching import match_client
```

Add this query constant beside the other module-level `text()` statements:

```python
# The matcher needs the sender, which lives on the message rather than in the
# extraction. Read inside the same transaction so it cannot disagree with what
# the rest of this write assumes.
_SENDER = text("SELECT sender_email FROM email_messages WHERE id = :id")
```

Then inside the `async with tenant_session(tenant_id) as session:` block (line 133), after the `codes = detect(...)` line at 153 and **before** the `for job in response.jobs:` loop at 158, insert:

```python
        # One client per email, not per vacancy: three vacancies in one mail
        # come from one company, and proposing three identical clients would
        # make the review queue unusable on the first busy day.
        sender_email = (
            await session.execute(_SENDER, {"id": email_message_id})
        ).scalar_one_or_none()
        first_company = next(
            (_value(job.company_name) for job in response.jobs if _value(job.company_name)),
            None,
        )
        await match_client(
            session, tenant_id, email_message_id, sender_email, first_company
        )
```

`_value` is the existing helper at `persist.py:110` that unwraps an extracted field and returns `None` when it is missing. Confirm the attribute name on the job object is `company_name` by reading `app/services/ingest/schema.py`; if it differs, use the real one — do not add an alias.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_client_ingestion.py tests/test_extract_job.py -v
```

Expected: all passed, including the pre-existing extraction tests — this step must not change what extraction stores.

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest -q
```

Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add app/services/ingest/persist.py tests/test_client_ingestion.py
git commit -m "Propose the client in the same transaction that found the job"
```

---

### Task 6: The API

**Files:**
- Create: `backend/app/api/clients.py`
- Modify: `backend/app/main.py:13,67-72`
- Test: `backend/tests/test_clients_api.py` (create)

**Interfaces:**
- Consumes: `Client`, `ClientMention` (Task 2); `settings.CLIENTS_PAGE_LIMIT` (Task 1); `_require_session` from `app.api.auth`.
- Produces: routes `GET /api/clients`, `GET /api/clients/{id}`, `POST /api/clients/{id}/confirm`, `POST /api/clients/{id}/merge`, `POST /api/clients/{id}/unmerge`, `POST /api/clients/{id}/archive`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_clients_api.py`. Copy the sign-in helper and tenant/user factory pattern from `tests/test_opportunities_api.py:54-193` rather than writing new ones.

```python
"""The list is a review queue before it is a directory.

Most rows in a young tenant are unconfirmed proposals, so the default view
excludes merged rows (which are no longer anyone's client) and the counts are
computed over the whole tenant rather than the page — a chip that shrank as
you paged would answer a different question than it appears to.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from tests.conftest import AdminSessionLocal
# Reuse, do not redefine: this is the same session cookie the real app reads.
from tests.test_opportunities_api import sign_in


@pytest.fixture
async def agency_with_clients():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    ids = {"live": uuid.uuid4(), "merged": uuid.uuid4()}
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'owner')"
            ),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, "
                "email_domain, status) VALUES (:i, :t, 'Acme', 'acme', 'acme.com', 'unconfirmed')"
            ),
            {"i": ids["live"], "t": tid},
        )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, status, "
                "merged_into_client_id) VALUES (:i, :t, 'Acme Old', 'acme old', 'merged', :w)"
            ),
            {"i": ids["merged"], "t": tid, "w": ids["live"]},
        )
        await s.commit()
    yield tid, uid, ids
    async with AdminSessionLocal() as s:
        await s.execute(text("DELETE FROM client_mentions WHERE tenant_id = :t"), {"t": tid})
        await s.execute(
            text("UPDATE clients SET merged_into_client_id = NULL WHERE tenant_id = :t"), {"t": tid}
        )
        await s.execute(text("DELETE FROM clients WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def test_the_list_hides_merged_rows_by_default(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["live"])]


async def test_the_status_filter_is_the_review_queue(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/clients?status=unconfirmed")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["live"])]
    assert body["counts"]["unconfirmed"] == 1


async def test_confirming_is_the_only_way_a_client_becomes_confirmed(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{ids['live']}/confirm")).status_code == 200
        body = (await http.get(f"/api/clients/{ids['live']}")).json()
    assert body["status"] == "confirmed"


async def test_unmerge_restores_a_wrongly_merged_client(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/clients/{ids['merged']}/unmerge")).status_code == 200
        body = (await http.get(f"/api/clients/{ids['merged']}")).json()
    assert body["status"] == "unconfirmed"
    assert body["merged_into_client_id"] is None


async def test_a_client_cannot_be_merged_into_itself(agency_with_clients) -> None:
    tid, uid, ids = agency_with_clients
    async with await _client_for(tid, uid) as http:
        r = await http.post(
            f"/api/clients/{ids['live']}/merge", json={"target_id": str(ids["live"])}
        )
    assert r.status_code == 400


async def test_one_agency_never_sees_anothers_clients(agency_with_clients) -> None:
    tid, uid, _ = agency_with_clients
    other_tid, other_uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": other_tid, "n": f"other-{other_tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": other_uid, "t": other_tid, "e": f"o{other_uid.hex[:6]}@other.sg"},
        )
        await s.commit()
    try:
        async with await _client_for(other_tid, other_uid) as http:
            body = (await http.get("/api/clients")).json()
        assert body["items"] == []
        assert body["total"] == 0
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": other_tid})
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": other_tid})
            await s.commit()
```

- [ ] **Step 2: Run them**

```bash
uv run pytest tests/test_clients_api.py -v
```

Expected: FAIL — 404 on every route.

- [ ] **Step 3: Implement the router**

Create `backend/app/api/clients.py`:

```python
"""The agency's client list — a review queue before it is a directory.

Every row here was proposed by the pipeline and is owned by a person. So the
write endpoints are all state transitions a human makes, and none of them is
something the matcher can do: confirm, archive, merge, unmerge. The matcher
creates and links; it never decides.

`unmerge` exists because merge is destructive to the mention graph and
recruiters will get it wrong. A merge with no way back is a merge people are
afraid to use, and an unused merge leaves the duplicates in the list.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select, update

from app.api.auth import _require_session
from app.core.config import settings
from app.db.rls import tenant_session
from app.models.client import Client, ClientMention

router = APIRouter(tags=["clients"])

StatusFilter = Literal["unconfirmed", "confirmed", "archived", "merged"]


class MergeRequest(BaseModel):
    target_id: uuid.UUID


def _serialize(client: Client) -> dict:
    return {
        "id": str(client.id),
        "name": client.name,
        "name_normalized": client.name_normalized,
        "email_domain": client.email_domain,
        "status": client.status,
        "merged_into_client_id": (
            str(client.merged_into_client_id) if client.merged_into_client_id else None
        ),
        "last_seen_at": client.last_seen_at.isoformat() if client.last_seen_at else None,
        "created_at": client.created_at.isoformat(),
    }


@router.get("/clients")
async def list_clients(
    request: Request,
    # Resolved in the body, not the signature: a default bound at import would
    # freeze the setting at the value it had when the module loaded.
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    status: StatusFilter | None = None,
) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    ceiling = settings.CLIENTS_PAGE_LIMIT
    page_limit = ceiling if limit is None else min(limit, ceiling)

    async with tenant_session(tenant_uuid) as session:
        # Counted over the whole tenant, before any filter or window. A count
        # that moved with the page would answer a different question than the
        # chip appears to ask.
        counts = {"all": 0}
        for stored, n in await session.execute(
            select(Client.status, func.count()).group_by(Client.status)
        ):
            counts["all"] += n
            counts[stored] = counts.get(stored, 0) + n

        base = select(Client)
        if status is not None:
            base = base.where(Client.status == status)
        else:
            # A merged row is no longer a client. It stays reachable by id and
            # by explicit filter so an unmerge is still possible.
            base = base.where(Client.status != Client.MERGED)

        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = (
            await session.execute(
                base.order_by(Client.last_seen_at.desc().nullslast(), Client.created_at.desc())
                .limit(page_limit)
                .offset(offset)
            )
        ).scalars().all()

    return {
        "items": [_serialize(c) for c in rows],
        "total": total,
        "limit": page_limit,
        "offset": offset,
        "counts": counts,
    }


@router.get("/clients/{client_id}")
async def get_client(request: Request, client_id: uuid.UUID) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        mentions = (
            await session.execute(
                select(ClientMention)
                .where(ClientMention.client_id == client_id)
                .order_by(ClientMention.created_at.desc())
            )
        ).scalars().all()

    payload = _serialize(client)
    payload["mentions"] = [
        {
            "id": str(m.id),
            "email_message_id": str(m.email_message_id) if m.email_message_id else None,
            "matched_by": m.matched_by,
            "created_at": m.created_at.isoformat(),
        }
        for m in mentions
    ]
    return payload


@router.post("/clients/{client_id}/confirm")
async def confirm_client(request: Request, client_id: uuid.UUID) -> dict:
    return await _transition(request, client_id, Client.CONFIRMED)


@router.post("/clients/{client_id}/archive")
async def archive_client(request: Request, client_id: uuid.UUID) -> dict:
    return await _transition(request, client_id, Client.ARCHIVED)


@router.post("/clients/{client_id}/merge")
async def merge_client(request: Request, client_id: uuid.UUID, body: MergeRequest) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    if body.target_id == client_id:
        raise HTTPException(status_code=400, detail="A client cannot be merged into itself")

    async with tenant_session(tenant_uuid) as session:
        loser = await _load(session, client_id)
        target = await _load(session, body.target_id)
        if target.status == Client.MERGED:
            # Merging into a merged row would build a chain the matcher only
            # follows one hop of. Point at the survivor instead.
            raise HTTPException(
                status_code=400, detail="Target is itself merged; merge into its target"
            )
        if loser.status == Client.MERGED:
            raise HTTPException(status_code=400, detail="Client is already merged")

        # Mentions move, because they are evidence about a company and the
        # company is now the target. Leaving them behind would make the
        # surviving row look newly discovered.
        await session.execute(
            update(ClientMention)
            .where(ClientMention.client_id == client_id)
            .values(client_id=body.target_id)
        )
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(status=Client.MERGED, merged_into_client_id=body.target_id)
        )
        await session.commit()
    return {"status": "merged", "merged_into_client_id": str(body.target_id)}


@router.post("/clients/{client_id}/unmerge")
async def unmerge_client(request: Request, client_id: uuid.UUID) -> dict:
    """Restore a merged client. Its mentions stay with the target.

    Deliberately partial: the mentions were rewritten by the merge and there is
    no record of which ones came from where, so returning the row to
    `unconfirmed` with no evidence is the honest outcome. Re-ingestion will
    re-attach anything still arriving.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status != Client.MERGED:
            raise HTTPException(status_code=400, detail="Client is not merged")
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(status=Client.UNCONFIRMED, merged_into_client_id=None)
        )
        await session.commit()
    return {"status": Client.UNCONFIRMED}


async def _transition(request: Request, client_id: uuid.UUID, status: str) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status == Client.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the client first")
        await session.execute(update(Client).where(Client.id == client_id).values(status=status))
        await session.commit()
    return {"status": status}


async def _load(session, client_id: uuid.UUID) -> Client:
    """Fetch inside the tenant session, so another agency's id is a 404.

    Not a 403: telling a caller that an id exists but is not theirs is itself
    a cross-tenant disclosure.
    """
    client = (
        await session.execute(select(Client).where(Client.id == client_id))
    ).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return client
```

- [ ] **Step 4: Register the router**

In `backend/app/main.py`, extend the import on line 13:

```python
from app.api import activity, auth, clients, glossary, graph_webhook, mailbox, opportunities
```

and add, among the `api.include_router()` calls at lines 67-72:

```python
api.include_router(clients.router)
```

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/test_clients_api.py tests/test_routing.py -v
```

Expected: all passed. `test_routing.py` proves no new route escaped `/api`.

- [ ] **Step 6: Commit**

```bash
git add app/api/clients.py app/main.py tests/test_clients_api.py
git commit -m "Give recruiters a queue for the clients the pipeline proposed"
```

---

### Task 7: Concurrency and the full suite

The one failure mode the earlier tasks cannot prove alone: two workers processing two messages from the same new domain at the same moment.

**Files:**
- Test: `backend/tests/test_client_concurrency.py` (create)

**Interfaces:**
- Consumes: `match_client` (Task 4).
- Produces: nothing.

- [ ] **Step 1: Write the test**

Create `backend/tests/test_client_concurrency.py`:

```python
"""Two workers, one new domain, one client.

The mailbox sync fans out across messages, so two extractions from the same
company can reach the matcher within microseconds of each other. Both will
find no existing client and both will insert. Without the `ON CONFLICT` on the
partial domain index the loser raises a unique violation, which fails an
extraction over a race that has an obvious correct answer.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.client_matching import match_client
from tests.conftest import AdminSessionLocal


@pytest.fixture
async def agency():
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    yield tid
    async with AdminSessionLocal() as s:
        await s.execute(text("DELETE FROM client_mentions WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM clients WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _match_once(tenant_id: uuid.UUID, sender: str) -> uuid.UUID | None:
    async with tenant_session(tenant_id) as session:
        cid = await match_client(session, tenant_id, None, sender, "Acme Pte Ltd")
        await session.commit()
        return cid


async def test_two_concurrent_matches_produce_one_client(agency) -> None:
    results = await asyncio.gather(
        _match_once(agency, "hr@acme.com.sg"),
        _match_once(agency, "jobs@acme.com.sg"),
        return_exceptions=True,
    )
    for r in results:
        assert not isinstance(r, Exception), f"concurrent match raised: {r!r}"

    async with tenant_session(agency) as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM clients WHERE email_domain = 'acme.com.sg'")
            )
        ).scalar_one()
    assert count == 1
```

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/test_client_concurrency.py -v
```

Expected: PASS if Task 4's `ON CONFLICT` is correct. If it raises `UniqueViolationError`, the conflict target does not match the partial index — fix `_INSERT_CLIENT` in `app/services/client_matching.py`, not this test.

- [ ] **Step 3: Run the full suite and the linter**

```bash
uv run pytest -q && uv run ruff check .
```

Expected: all passed, no lint findings.

- [ ] **Step 4: Verify the migration reverses**

```bash
uv run alembic downgrade -1 && uv run alembic upgrade head
```

Expected: both succeed. A migration that cannot be reversed cannot be safely deployed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_client_concurrency.py
git commit -m "Prove two workers on one domain make one client"
```

---

## What this plan does not build

Recorded so a reader does not mistake absence for oversight:

- **Candidate profiles.** `ExtractedJob` (`app/services/ingest/schema.py:72`) has no candidate fields, so there is nothing to propose one from. Extending the extraction schema and prompt is its own spec.
- **The retention purge.** `mailboxes.retention_months` and `email_messages.retention_until` are written by ingestion and read by nothing — no purge job exists anywhere in the codebase. That is a pre-existing gap, not one this feature creates. `client_mentions.email_message_id` uses `ON DELETE SET NULL` so it will survive that purge when it is built.
- **Linking opportunities to clients.** No `client_id` on `opportunities`. The obvious next step, but it belongs with the UI that would use it.
- **A split operation.** Two companies genuinely sharing one mail domain collapse into one client; a recruiter archives the wrong row and creates the sibling by hand.
