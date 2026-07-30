# Job Order Assignment and Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every job order an assigned recruiter, derived from the client's assignee, and let a recruiter share it with named colleagues or the whole agency without duplicating the row.

**Architecture:** Four schema changes (a composite-key prerequisite on `users`, an assignee on `clients`, three new columns on `opportunities`, and two new tables) plus one new service module holding the visibility predicate that every read must pass through. Sharing grants read-only sight on the canonical row; editing stays with the single assignee. Notifications gain a recipient list so events stop fanning out tenant-wide.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Alembic, Postgres 16 with RLS, pytest (`asyncio_mode = "auto"`), `uv`.

**Spec:** [2026-07-30-job-order-assignment-and-sharing-design.md](../specs/2026-07-30-job-order-assignment-and-sharing-design.md)

## Global Constraints

- All commands run from `backend/`. Tests: `uv run pytest`. Lint: `uv run ruff check .`.
- **No hardcoded values.** Config comes from the repo-root `.env` via `app.core.config.settings`.
- Every business table carries `tenant_id` via the `TenantScoped` mixin (§18).
- **Every new tenant-scoped table MUST get `ENABLE` + `FORCE ROW LEVEL SECURITY` and a `tenant_isolation` policy in its migration.** `verify_rls_enforced` (`app/db/rls.py:58`) refuses to boot the service otherwise. Copy `_enforce_rls()` from `alembic/versions/20260730_0740_client_administration.py:77-100` verbatim, changing only the `PROTECTED` tuple.
- Every user foreign key is **composite** — `(tenant_id, <col>)` → `(users.tenant_id, users.id)`. A plain `users.id` reference lets a row cross agencies.
- No single file exceeds 1500 LOC. `app/api/opportunities.py` is 691 and `app/api/clients.py` is 867; watch both.
- Tests never run against the live database — `tests/conftest.py` refuses a non-local host.
- Every route lives under `/api` (`tests/test_routing.py` enforces this).

---

### Task 0: Resolve the divergent Alembic history

`alembic heads` currently reports **two** heads: `1519048c9751` (`20260728_1700_candidate_avatar.py`) and `8c7e0f3c5305` (`20260730_1006_client_logo.py`). Every later task adds a migration, and adding one on top of a branched history produces a third head that will not apply.

**Files:**
- Create (only if two heads are confirmed): `backend/alembic/versions/<generated>_merge_heads.py`

**Interfaces:**
- Produces: a single Alembic head revision id, used as `down_revision` by Task 1.

- [ ] **Step 1: Confirm the head count**

`alembic` needs `DATABASE_URL`, so load the repo-root `.env` first:

```bash
set -a && . ../.env && set +a && uv run alembic heads
```

Expected: either one line (no branch — skip to Step 4) or two lines.

- [ ] **Step 2: If two heads, merge them**

```bash
set -a && . ../.env && set +a && uv run alembic merge -m "merge heads" 1519048c9751 8c7e0f3c5305
```

- [ ] **Step 3: Verify a single head**

```bash
set -a && . ../.env && set +a && uv run alembic heads
```

Expected: exactly one revision id. **Record it — every later task's first migration uses it as `down_revision`.**

- [ ] **Step 4: Confirm the suite still passes before any change**

```bash
uv run pytest -q
```

Expected: all pass. This is the baseline; a failure here is pre-existing and must be understood before continuing.

- [ ] **Step 5: Commit (skip if no merge file was created)**

```bash
git add alembic/versions/
git commit -m "Rejoin the two migration heads before building on them"
```

---

### Task 1: Give `users` a composite key

Nothing else can be built first. `users` has no `UniqueConstraint(tenant_id, id)`, so no composite foreign key can reference it, so every user reference in this feature would be a plain `users.id` that can cross tenants.

**Files:**
- Modify: `backend/app/models/tenant.py:37-48` (the `User.__table_args__` tuple)
- Create: `backend/alembic/versions/<generated>_users_composite_key.py`
- Create: `backend/tests/test_users_composite_key.py`

**Interfaces:**
- Produces: `uq_users_tenant_id_id` on `users(tenant_id, id)`. Tasks 2, 3 and 4 declare composite FKs against it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_users_composite_key.py`:

```python
"""The constraint every composite user foreign key in this feature needs.

Without it a share row, a client assignee or an opportunity assignee could
name a user in another agency, and the tenant boundary would hold only in
application code.
"""

from sqlalchemy import text

from app.db.session import AdminSessionLocal


async def test_users_has_tenant_id_id_unique_constraint() -> None:
    async with AdminSessionLocal() as session:
        found = (
            await session.execute(
                text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'uq_users_tenant_id_id' "
                    "AND conrelid = 'users'::regclass"
                )
            )
        ).scalar_one_or_none()

    assert found == 1, "uq_users_tenant_id_id is missing; composite FKs cannot reference users"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_users_composite_key.py -v
```

Expected: FAIL — `AssertionError: uq_users_tenant_id_id is missing`.

- [ ] **Step 3: Add the constraint to the model**

In `backend/app/models/tenant.py`, add one entry to `User.__table_args__`, immediately after the two existing `UniqueConstraint` lines:

```python
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        UniqueConstraint("tenant_id", "ms_object_id", name="uq_users_tenant_ms_object_id"),
        # Children reference (tenant_id, id) so their FK cannot cross agencies —
        # the same idiom `clients` carries as `uq_clients_tenant_id_id`. Declared
        # here as well as in the migration so autogenerate does not propose
        # dropping it.
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id_id"),
        Index(
            "uq_users_one_owner_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
        ),
    )
```

- [ ] **Step 4: Write the migration**

```bash
set -a && . ../.env && set +a && uv run alembic revision -m "users composite key"
```

Open the generated file and set its body (leave the generated `revision` alone; set `down_revision` to the single head from Task 0):

```python
def upgrade() -> None:
    op.create_unique_constraint("uq_users_tenant_id_id", "users", ["tenant_id", "id"])


def downgrade() -> None:
    op.drop_constraint("uq_users_tenant_id_id", "users", type_="unique")
```

- [ ] **Step 5: Apply it and run the test**

```bash
set -a && . ../.env && set +a && uv run alembic upgrade head && uv run pytest tests/test_users_composite_key.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models/tenant.py alembic/versions/ tests/test_users_composite_key.py
git commit -m "Let a user be referenced without leaving their agency"
```

---

### Task 2: Assign a client to a recruiter

**Files:**
- Modify: `backend/app/models/client.py` (add `assigned_user_id` to `Client`; add a `ClientCollaborator` class at the end)
- Create: `backend/alembic/versions/<generated>_client_assignment.py`
- Modify: `backend/tests/conftest.py:96-108` (`_CLEANUP_STATEMENTS`)
- Create: `backend/tests/test_client_assignment.py`

**Interfaces:**
- Consumes: `uq_users_tenant_id_id` (Task 1).
- Produces: `Client.assigned_user_id: Mapped[uuid.UUID | None]`; `ClientCollaborator` with `client_id`, `user_id`. Task 3 reads `Client.assigned_user_id`; Task 9 writes both.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_client_assignment.py`:

```python
"""A client belongs to a recruiter, and that reference cannot leave the agency."""

import uuid

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.models.client import Client, ClientCollaborator
from tests.conftest import cleanup_tenant, seed_tenant_with_user


async def test_client_can_be_assigned_to_a_user() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    client_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(Client).values(
                    id=client_id,
                    tenant_id=tenant_id,
                    name="Acme Pte Ltd",
                    name_normalized="acme",
                    assigned_user_id=user_id,
                )
            )
        async with tenant_session(tenant_id) as session:
            assigned = (
                await session.execute(
                    select(Client.assigned_user_id).where(Client.id == client_id)
                )
            ).scalar_one()
        assert assigned == user_id
    finally:
        await cleanup_tenant(tenant_id)


async def test_assignee_from_another_tenant_is_refused() -> None:
    tenant_a, _user_a = await seed_tenant_with_user()
    tenant_b, user_b = await seed_tenant_with_user()
    try:
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_a) as session:
                await session.execute(
                    insert(Client).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_a,
                        name="Acme Pte Ltd",
                        name_normalized="acme",
                        assigned_user_id=user_b,  # belongs to tenant B
                    )
                )
    finally:
        await cleanup_tenant(tenant_a, tenant_b)


async def test_collaborator_is_unique_per_client_and_user() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    client_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(Client).values(
                    id=client_id,
                    tenant_id=tenant_id,
                    name="Acme Pte Ltd",
                    name_normalized="acme",
                )
            )
            await session.execute(
                insert(ClientCollaborator).values(
                    id=uuid.uuid4(), tenant_id=tenant_id, client_id=client_id, user_id=user_id
                )
            )
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(
                    insert(ClientCollaborator).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        client_id=client_id,
                        user_id=user_id,
                    )
                )
    finally:
        await cleanup_tenant(tenant_id)
```

- [ ] **Step 2: Add the `seed_tenant_with_user` helper to conftest**

`backend/tests/conftest.py` has `cleanup_tenant` but no seeding helper shared across files. Add one after `cleanup_tenant` (around line 127):

```python
async def seed_tenant_with_user(role: str = "recruiter") -> tuple[uuid.UUID, uuid.UUID]:
    """One tenant and one user in it. Returns (tenant_id, user_id).

    Uses the admin session because creating the tenant is what makes the
    RLS-scoped session possible in the first place.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with AdminSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) "
                "VALUES (:id, :name, :slug)"
            ),
            {"id": tenant_id, "name": f"Agency {tenant_id.hex[:8]}", "slug": tenant_id.hex[:12]},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:id, :tenant_id, :email, :role)"
            ),
            {
                "id": user_id,
                "tenant_id": tenant_id,
                "email": f"{user_id.hex[:8]}@example.test",
                "role": role,
            },
        )
        await session.commit()
    return tenant_id, user_id
```

Add the two new tables to `_CLEANUP_STATEMENTS`, **before** the `DELETE FROM clients` line (order matters — children first):

```python
_CLEANUP_STATEMENTS = (
    "DELETE FROM opportunity_shares WHERE tenant_id = :t",
    "DELETE FROM client_collaborators WHERE tenant_id = :t",
    "DELETE FROM client_contacts WHERE tenant_id = :t",
    "DELETE FROM client_mentions WHERE tenant_id = :t",
    ...
```

(`opportunity_shares` arrives in Task 4; adding both now costs nothing because `cleanup_tenant` swallows the error for a table that does not exist yet.)

- [ ] **Step 3: Run the tests and watch them fail**

```bash
uv run pytest tests/test_client_assignment.py -v
```

Expected: FAIL — `ImportError: cannot import name 'ClientCollaborator'`.

- [ ] **Step 4: Add the column and the model**

In `backend/app/models/client.py`, add to `Client` after the `source` column (line 91):

```python
    # The recruiter who takes care of this account. Nullable in both
    # directions of the word: a pipeline-proposed client arrives with nobody
    # on it, and a departing recruiter's clients must outlive the account
    # rather than vanishing with it.
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
```

Add to `Client.__table_args__`, after the existing `ForeignKeyConstraint`:

```python
        ForeignKeyConstraint(
            ["tenant_id", "assigned_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_clients_assignee_same_tenant",
            ondelete="SET NULL",
        ),
```

Append the new class at the end of the file:

```python
class ClientCollaborator(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A recruiter who covers this account besides the primary.

    Deliberately grants nothing. This is a record of who else knows the
    client, not a share: making it an implicit grant on the client's job
    orders would put a second, invisible path into the visibility predicate,
    and then "why can Raj see this?" would have two possible answers. Cover
    that needs sight of the work is an explicit share or a reassignment.

    There is no `is_primary` flag — the primary lives on `clients.assigned_user_id`,
    so there is one place to read it and no way for the two to disagree.
    """

    __tablename__ = "client_collaborators"

    client_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_client_collaborators_client_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_client_collaborators_user_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "client_id", "user_id", name="uq_client_collaborators_once"
        ),
    )
```

- [ ] **Step 5: Write the migration**

```bash
set -a && . ../.env && set +a && uv run alembic revision -m "client assignment"
```

Body (keep the generated `revision`; `down_revision` is Task 1's revision):

```python
SETTING = "app.tenant_id"
PROTECTED = (("client_collaborators", "tenant_id"),)


def upgrade() -> None:
    op.add_column("clients", sa.Column("assigned_user_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_clients_assignee_same_tenant",
        "clients",
        "users",
        ["tenant_id", "assigned_user_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "client_collaborators",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_client_collaborators_client_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_client_collaborators_user_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "client_id", "user_id", name="uq_client_collaborators_once"
        ),
    )
    op.create_index(
        op.f("ix_client_collaborators_client_id"), "client_collaborators", ["client_id"]
    )
    op.create_index(op.f("ix_client_collaborators_user_id"), "client_collaborators", ["user_id"])
    op.create_index(
        op.f("ix_client_collaborators_tenant_id"), "client_collaborators", ["tenant_id"]
    )

    _enforce_rls()


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
    op.drop_table("client_collaborators")
    op.drop_constraint("fk_clients_assignee_same_tenant", "clients", type_="foreignkey")
    op.drop_column("clients", "assigned_user_id")
```

- [ ] **Step 6: Apply and verify**

```bash
set -a && . ../.env && set +a && uv run alembic upgrade head && uv run pytest tests/test_client_assignment.py -v
```

Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add app/models/client.py alembic/versions/ tests/
git commit -m "Give a client the recruiter who takes care of it"
```

---

### Task 3: Give a job order a client, an assignee, and a life without email

**Files:**
- Modify: `backend/app/models/opportunity.py:36-45` (`email_message_id`), plus new columns and `__table_args__`
- Create: `backend/alembic/versions/<generated>_opportunity_assignment.py`
- Create: `backend/tests/test_opportunity_assignment.py`

**Interfaces:**
- Consumes: `uq_users_tenant_id_id` (Task 1), `uq_clients_tenant_id_id` (already exists).
- Produces: `Opportunity.client_id`, `Opportunity.assigned_user_id`, `Opportunity.source` (`"pipeline"` | `"manual"`), and a nullable `email_message_id`. Tasks 5, 6, 8 and 10 all read these.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_opportunity_assignment.py`:

```python
"""A job order can exist without an email, and knows whose it is."""

import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.db.session import AdminSessionLocal
from app.models.opportunity import Opportunity
from tests.conftest import cleanup_tenant, seed_tenant_with_user


async def test_manual_opportunity_needs_no_email() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(Opportunity).values(
                    id=opportunity_id,
                    tenant_id=tenant_id,
                    email_message_id=None,
                    source=Opportunity.MANUAL,
                    assigned_user_id=user_id,
                    job_title_raw="Warehouse Assistant",
                )
            )
        async with tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(Opportunity.source, Opportunity.assigned_user_id).where(
                        Opportunity.id == opportunity_id
                    )
                )
            ).one()
        assert row.source == "manual"
        assert row.assigned_user_id == user_id
    finally:
        await cleanup_tenant(tenant_id)


async def test_source_vocabulary_is_pinned() -> None:
    tenant_id, _user_id = await seed_tenant_with_user()
    try:
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(
                    insert(Opportunity).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        email_message_id=None,
                        source="shared",  # never a valid source: sharing creates no row
                    )
                )
    finally:
        await cleanup_tenant(tenant_id)


async def test_deleting_the_assignee_queues_the_job_order() -> None:
    """SET NULL, not CASCADE: a recruiter leaving must not delete the work."""
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = uuid.uuid4()
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(Opportunity).values(
                    id=opportunity_id,
                    tenant_id=tenant_id,
                    email_message_id=None,
                    source=Opportunity.MANUAL,
                    assigned_user_id=user_id,
                )
            )
        async with AdminSessionLocal() as session:
            await session.execute(
                text("DELETE FROM users WHERE id = :u"), {"u": user_id}
            )
            await session.commit()
        async with tenant_session(tenant_id) as session:
            assigned = (
                await session.execute(
                    select(Opportunity.assigned_user_id).where(
                        Opportunity.id == opportunity_id
                    )
                )
            ).scalar_one()
        assert assigned is None
    finally:
        await cleanup_tenant(tenant_id)
```

- [ ] **Step 2: Run and watch it fail**

```bash
uv run pytest tests/test_opportunity_assignment.py -v
```

Expected: FAIL — `AttributeError: type object 'Opportunity' has no attribute 'MANUAL'`.

- [ ] **Step 3: Change the model**

In `backend/app/models/opportunity.py`, add the source constants beside the placement ones (after line 34):

```python
    PIPELINE = "pipeline"
    MANUAL = "manual"
    SOURCES = (PIPELINE, MANUAL)
```

Replace the `email_message_id` column (lines 36-41) with:

```python
    # Nullable since a job order may be taken over the phone or WhatsApp, and
    # SET NULL rather than CASCADE because once a job order can be assigned,
    # shared and worked on, a retention purge of the mail body must not delete
    # it. `Client.first_seen_email_message_id` makes the same argument.
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("email_messages.id", ondelete="SET NULL"),
        index=True,
    )
```

Add after the `received_datetime` column:

```python
    # Which client this vacancy is for. Without it, `assigned_user_id` is a
    # copied user id with no record of what drove it: reassigning a client
    # could not find its job orders, and a manual job order would have no
    # client at all.
    client_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), index=True)

    # The recruiter responsible. NULL is the unassigned queue — visible to
    # everyone and claimable, not hidden. Set at ingestion from the client's
    # assignee, never from whose mailbox the mail happened to land in.
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), index=True
    )

    # How the row came to exist. Not inferable from `email_message_id`: that
    # column is now ON DELETE SET NULL, so a retention purge would otherwise
    # silently reclassify a pipeline job order as manual — the same argument
    # `Client.source` makes. There is no 'shared' value: sharing grants sight
    # of the existing row and never creates one.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=PIPELINE)
```

Add three entries to `__table_args__`:

```python
        CheckConstraint(
            "source IN ('pipeline', 'manual')",
            name="ck_opportunities_source_known",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_opportunities_client_same_tenant",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assigned_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_opportunities_assignee_same_tenant",
            ondelete="SET NULL",
        ),
```

Add `ForeignKeyConstraint` to the imports from `sqlalchemy` at the top of the file.

- [ ] **Step 4: Write the migration**

```bash
set -a && . ../.env && set +a && uv run alembic revision -m "opportunity assignment"
```

Body:

```python
def upgrade() -> None:
    op.add_column("opportunities", sa.Column("client_id", sa.UUID(), nullable=True))
    op.add_column("opportunities", sa.Column("assigned_user_id", sa.UUID(), nullable=True))
    # NOT NULL on a table with existing rows, so it needs a server default at
    # add-time — the same reason `clients.source` carries one.
    op.add_column(
        "opportunities",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="pipeline"),
    )
    op.create_index(op.f("ix_opportunities_client_id"), "opportunities", ["client_id"])
    op.create_index(
        op.f("ix_opportunities_assigned_user_id"), "opportunities", ["assigned_user_id"]
    )
    op.create_check_constraint(
        "ck_opportunities_source_known", "opportunities", "source IN ('pipeline', 'manual')"
    )
    op.create_foreign_key(
        "fk_opportunities_client_same_tenant",
        "opportunities",
        "clients",
        ["tenant_id", "client_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_opportunities_assignee_same_tenant",
        "opportunities",
        "users",
        ["tenant_id", "assigned_user_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL",
    )

    # Backfill the client from the evidence already recorded per message.
    # Rows whose mention is gone stay NULL, which is honest: the link existed
    # and the record of it does not.
    op.execute(
        """
        UPDATE opportunities o
           SET client_id = m.client_id
          FROM client_mentions m
         WHERE m.email_message_id = o.email_message_id
           AND m.tenant_id = o.tenant_id
        """
    )

    # email_message_id: NOT NULL -> nullable, CASCADE -> SET NULL.
    op.alter_column("opportunities", "email_message_id", nullable=True)
    op.drop_constraint(
        "opportunities_email_message_id_fkey", "opportunities", type_="foreignkey"
    )
    op.create_foreign_key(
        "opportunities_email_message_id_fkey",
        "opportunities",
        "email_messages",
        ["email_message_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "opportunities_email_message_id_fkey", "opportunities", type_="foreignkey"
    )
    op.create_foreign_key(
        "opportunities_email_message_id_fkey",
        "opportunities",
        "email_messages",
        ["email_message_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute("DELETE FROM opportunities WHERE email_message_id IS NULL")
    op.alter_column("opportunities", "email_message_id", nullable=False)
    op.drop_constraint(
        "fk_opportunities_assignee_same_tenant", "opportunities", type_="foreignkey"
    )
    op.drop_constraint("fk_opportunities_client_same_tenant", "opportunities", type_="foreignkey")
    op.drop_constraint("ck_opportunities_source_known", "opportunities", type_="check")
    op.drop_column("opportunities", "source")
    op.drop_column("opportunities", "assigned_user_id")
    op.drop_column("opportunities", "client_id")
```

**Before running this, verify the real FK constraint name** — `opportunities_email_message_id_fkey` is Postgres's default and is probably right, but the migration drops it by name and will fail loudly if it differs:

```bash
set -a && . ../.env && set +a && uv run python -c "
import asyncio, sqlalchemy as sa
from app.db.session import AdminSessionLocal
async def main():
    async with AdminSessionLocal() as s:
        print((await s.execute(sa.text(
            \"SELECT conname FROM pg_constraint WHERE conrelid='opportunities'::regclass \"
            \"AND contype='f'\"))).scalars().all())
asyncio.run(main())"
```

Use whatever name it prints in place of `opportunities_email_message_id_fkey`.

- [ ] **Step 5: Apply and verify**

```bash
set -a && . ../.env && set +a && uv run alembic upgrade head && uv run pytest tests/test_opportunity_assignment.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Run the whole suite — this task changes a NOT NULL column**

```bash
uv run pytest -q
```

Expected: all pass. Existing ingestion tests insert `email_message_id`, so nullability does not break them, but the `source` default and the new FKs touch every opportunity insert.

- [ ] **Step 7: Commit**

```bash
git add app/models/opportunity.py alembic/versions/ tests/
git commit -m "Let a job order have an owner, a client, and no email at all"
```

---

### Task 4: The share table

**Files:**
- Create: `backend/app/models/opportunity_share.py`
- Modify: `backend/app/models/__init__.py` (register it)
- Create: `backend/alembic/versions/<generated>_opportunity_shares.py`
- Create: `backend/tests/test_opportunity_shares_model.py`

**Interfaces:**
- Consumes: `uq_users_tenant_id_id` (Task 1), `Opportunity` (Task 3).
- Produces: `OpportunityShare` with `SCOPE_USER = "user"`, `SCOPE_TENANT = "tenant"`, and columns `opportunity_id`, `scope`, `shared_with_user_id`, `shared_by_user_id`, `note`. Tasks 5 and 7 use it.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_opportunity_shares_model.py`:

```python
"""A grant of sight on someone else's job order."""

import uuid

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from app.db.session import AdminSessionLocal
from app.models.opportunity import Opportunity
from app.models.opportunity_share import OpportunityShare
from tests.conftest import cleanup_tenant, seed_tenant_with_user


async def _an_opportunity(tenant_id: uuid.UUID) -> uuid.UUID:
    opportunity_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        await session.execute(
            insert(Opportunity).values(
                id=opportunity_id,
                tenant_id=tenant_id,
                email_message_id=None,
                source=Opportunity.MANUAL,
            )
        )
    return opportunity_id


async def test_tenant_scope_forbids_a_target_user() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = await _an_opportunity(tenant_id)
    try:
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(
                    insert(OpportunityShare).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        opportunity_id=opportunity_id,
                        scope=OpportunityShare.SCOPE_TENANT,
                        shared_with_user_id=user_id,  # must be NULL for tenant scope
                    )
                )
    finally:
        await cleanup_tenant(tenant_id)


async def test_user_scope_requires_a_target_user() -> None:
    tenant_id, _user_id = await seed_tenant_with_user()
    opportunity_id = await _an_opportunity(tenant_id)
    try:
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(
                    insert(OpportunityShare).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        opportunity_id=opportunity_id,
                        scope=OpportunityShare.SCOPE_USER,
                        shared_with_user_id=None,
                    )
                )
    finally:
        await cleanup_tenant(tenant_id)


async def test_deleting_the_recipient_deletes_the_share() -> None:
    """CASCADE, not SET NULL.

    SET NULL would turn a user share into a tenant broadcast, and would
    violate ck_opportunity_shares_scope_target — making the user DELETE fail
    outright rather than merely doing the wrong thing.
    """
    tenant_id, owner_id = await seed_tenant_with_user()
    _t, recipient_id = await seed_tenant_with_user()
    # Put the recipient in the same tenant as the owner.
    async with AdminSessionLocal() as session:
        await session.execute(
            text("UPDATE users SET tenant_id = :t WHERE id = :u"),
            {"t": tenant_id, "u": recipient_id},
        )
        await session.commit()
    opportunity_id = await _an_opportunity(tenant_id)
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(OpportunityShare).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_USER,
                    shared_with_user_id=recipient_id,
                    shared_by_user_id=owner_id,
                )
            )
        async with AdminSessionLocal() as session:
            await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": recipient_id})
            await session.commit()
        async with tenant_session(tenant_id) as session:
            remaining = (
                await session.execute(
                    select(OpportunityShare.id).where(
                        OpportunityShare.opportunity_id == opportunity_id
                    )
                )
            ).scalars().all()
        assert remaining == []
    finally:
        await cleanup_tenant(tenant_id)


async def test_resharing_to_the_same_user_is_refused_by_the_index() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    opportunity_id = await _an_opportunity(tenant_id)
    values = dict(
        tenant_id=tenant_id,
        opportunity_id=opportunity_id,
        scope=OpportunityShare.SCOPE_USER,
        shared_with_user_id=user_id,
    )
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(insert(OpportunityShare).values(id=uuid.uuid4(), **values))
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant_id) as session:
                await session.execute(insert(OpportunityShare).values(id=uuid.uuid4(), **values))
    finally:
        await cleanup_tenant(tenant_id)
```

- [ ] **Step 2: Run and watch it fail**

```bash
uv run pytest tests/test_opportunity_shares_model.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.opportunity_share'`.

- [ ] **Step 3: Write the model**

Create `backend/app/models/opportunity_share.py`:

```python
"""One grant of sight on one job order.

Sharing never copies. A forked opportunity would make the same vacancy exist
twice, and every count, dedup and "who filled it" answer would become
ambiguous — so a share is a row that says who may see the canonical one.

There is no `access` column. Every share is read: exactly one person, the
assignee, can edit a job order, which leaves no permission lattice to reason
about.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class OpportunityShare(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "opportunity_shares"

    SCOPE_USER = "user"
    SCOPE_TENANT = "tenant"
    SCOPES = (SCOPE_USER, SCOPE_TENANT)

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)

    # NULL if and only if scope='tenant' — one broadcast row rather than one
    # row per colleague, so a recruiter hired next month inherits it.
    shared_with_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), index=True
    )
    shared_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "scope IN ('user', 'tenant')", name="ck_opportunity_shares_scope_known"
        ),
        # The pairing rule. Same idiom as
        # `ck_opportunities_sex_requirement_has_reason`.
        CheckConstraint(
            "(scope = 'tenant') = (shared_with_user_id IS NULL)",
            name="ck_opportunity_shares_scope_target",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.id"],
            name="fk_opportunity_shares_opportunity_same_tenant",
            ondelete="CASCADE",
        ),
        # CASCADE: a share to a deleted user is meaningless, and SET NULL
        # would both convert it into a tenant broadcast and violate
        # ck_opportunity_shares_scope_target — making the user DELETE fail.
        ForeignKeyConstraint(
            ["tenant_id", "shared_with_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_opportunity_shares_recipient_same_tenant",
            ondelete="CASCADE",
        ),
        # SET NULL, for the opposite reason: the fact that someone shared this
        # must outlive the account that did.
        ForeignKeyConstraint(
            ["tenant_id", "shared_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_opportunity_shares_sharer_same_tenant",
            ondelete="SET NULL",
        ),
        # Re-sharing updates rather than duplicating. Two partial indexes
        # rather than one constraint, because the two scopes have different
        # uniqueness: one row per recipient, and one broadcast per job order.
        Index(
            "uq_opportunity_shares_per_user",
            "tenant_id",
            "opportunity_id",
            "shared_with_user_id",
            unique=True,
            postgresql_where=text("scope = 'user'"),
        ),
        Index(
            "uq_opportunity_shares_per_tenant",
            "tenant_id",
            "opportunity_id",
            unique=True,
            postgresql_where=text("scope = 'tenant'"),
        ),
    )
```

Register it in `backend/app/models/__init__.py` alongside the existing model imports, so Alembic autogenerate and the metadata see it.

- [ ] **Step 4: Write the migration**

```bash
set -a && . ../.env && set +a && uv run alembic revision -m "opportunity shares"
```

Body — `op.create_table` mirroring the model exactly, the two partial indexes via `op.create_index(..., postgresql_where=sa.text("scope = 'user'"))`, then the same `_enforce_rls()` helper copied from Task 2 with `PROTECTED = (("opportunity_shares", "tenant_id"),)`.

- [ ] **Step 5: Apply and verify**

```bash
set -a && . ../.env && set +a && uv run alembic upgrade head && uv run pytest tests/test_opportunity_shares_model.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Verify RLS is actually forced — the boot check depends on it**

```bash
uv run pytest tests/ -k rls -v
```

Expected: pass. A new tenant table without `FORCE ROW LEVEL SECURITY` fails `verify_rls_enforced` at startup, not at test time, so this is the cheapest place to catch it.

- [ ] **Step 7: Commit**

```bash
git add app/models/ alembic/versions/ tests/
git commit -m "Let a job order be shown to a colleague without being copied"
```

---

### Task 5: The visibility predicate

The single place the rule lives. Everything after this consumes it.

**Files:**
- Create: `backend/app/services/visibility.py`
- Create: `backend/tests/test_visibility.py`

**Interfaces:**
- Consumes: `Opportunity` (Task 3), `OpportunityShare` (Task 4), `User.role`.
- Produces:
  - `visible_opportunities(user_id: uuid.UUID, role: str) -> ColumnElement[bool]`
  - `can_edit(opportunity_row, user_id: uuid.UUID, role: str) -> bool`
  - `async load_visible_opportunity(session, opportunity_id, user_id, role) -> Opportunity` — raises `HTTPException(404)`
  - `async load_editable_opportunity(session, opportunity_id, user_id, role) -> Opportunity` — raises 404 if invisible, 403 if visible but not editable
  - `OWNER_ROLE = "owner"`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_visibility.py`. Six cases, one per term of the predicate:

```python
"""Who may see a job order, term by term.

RLS covers the tenant boundary; this predicate covers the boundary between
recruiters inside one agency, and it lives in application code — so it is
tested exhaustively rather than trusted.
"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import insert, select, text

from app.db.rls import tenant_session
from app.db.session import AdminSessionLocal
from app.models.opportunity import Opportunity
from app.models.opportunity_share import OpportunityShare
from app.services.visibility import (
    can_edit,
    load_editable_opportunity,
    load_visible_opportunity,
    visible_opportunities,
)
from tests.conftest import cleanup_tenant, seed_tenant_with_user


async def _second_user(tenant_id: uuid.UUID, role: str = "recruiter") -> uuid.UUID:
    user_id = uuid.uuid4()
    async with AdminSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:id, :t, :e, :r)"
            ),
            {"id": user_id, "t": tenant_id, "e": f"{user_id.hex[:8]}@example.test", "r": role},
        )
        await session.commit()
    return user_id


async def _opportunity(tenant_id: uuid.UUID, **values) -> uuid.UUID:
    opportunity_id = uuid.uuid4()
    async with tenant_session(tenant_id) as session:
        await session.execute(
            insert(Opportunity).values(
                id=opportunity_id,
                tenant_id=tenant_id,
                email_message_id=None,
                source=Opportunity.MANUAL,
                **values,
            )
        )
    return opportunity_id


async def _visible_ids(tenant_id, user_id, role) -> set[uuid.UUID]:
    async with tenant_session(tenant_id) as session:
        rows = await session.execute(
            select(Opportunity.id).where(visible_opportunities(user_id, role))
        )
    return set(rows.scalars().all())


async def test_unassigned_is_visible_to_everyone() -> None:
    tenant_id, user_id = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        assert opportunity_id in await _visible_ids(tenant_id, other, "recruiter")
        assert opportunity_id in await _visible_ids(tenant_id, user_id, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_assigned_is_hidden_from_everyone_else() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        assert opportunity_id in await _visible_ids(tenant_id, mine, "recruiter")
        assert opportunity_id not in await _visible_ids(tenant_id, other, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_user_share_reveals_it() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(OpportunityShare).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_USER,
                    shared_with_user_id=other,
                    shared_by_user_id=mine,
                )
            )
        assert opportunity_id in await _visible_ids(tenant_id, other, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_tenant_share_reveals_it_to_a_user_who_did_not_exist_yet() -> None:
    """The reason a broadcast is one row and not N."""
    tenant_id, mine = await seed_tenant_with_user()
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(OpportunityShare).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_TENANT,
                    shared_with_user_id=None,
                    shared_by_user_id=mine,
                )
            )
        newcomer = await _second_user(tenant_id)  # hired after the broadcast
        assert opportunity_id in await _visible_ids(tenant_id, newcomer, "recruiter")
    finally:
        await cleanup_tenant(tenant_id)


async def test_the_owner_sees_everything() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    boss = await _second_user(tenant_id, role="owner")
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        assert opportunity_id in await _visible_ids(tenant_id, boss, "owner")
    finally:
        await cleanup_tenant(tenant_id)


async def test_an_invisible_job_order_is_404_not_403() -> None:
    """A 403 would confirm the row exists."""
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        async with tenant_session(tenant_id) as session:
            with pytest.raises(HTTPException) as exc:
                await load_visible_opportunity(session, opportunity_id, other, "recruiter")
        assert exc.value.status_code == 404
    finally:
        await cleanup_tenant(tenant_id)


async def test_a_share_recipient_may_read_but_not_edit() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=mine)
    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                insert(OpportunityShare).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_USER,
                    shared_with_user_id=other,
                    shared_by_user_id=mine,
                )
            )
        async with tenant_session(tenant_id) as session:
            await load_visible_opportunity(session, opportunity_id, other, "recruiter")
            with pytest.raises(HTTPException) as exc:
                await load_editable_opportunity(session, opportunity_id, other, "recruiter")
        # Visible, so hiding its existence would be theatre.
        assert exc.value.status_code == 403
    finally:
        await cleanup_tenant(tenant_id)


async def test_unassigned_is_readable_by_all_and_editable_by_none() -> None:
    tenant_id, _mine = await seed_tenant_with_user()
    other = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        async with tenant_session(tenant_id) as session:
            row = await load_visible_opportunity(session, opportunity_id, other, "recruiter")
            assert can_edit(row, other, "recruiter") is False
    finally:
        await cleanup_tenant(tenant_id)
```

- [ ] **Step 2: Run and watch it fail**

```bash
uv run pytest tests/test_visibility.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.visibility'`.

- [ ] **Step 3: Write the module**

Create `backend/app/services/visibility.py`:

```python
"""Who, inside one agency, may see and edit a job order.

RLS enforces the boundary between agencies and always will: that boundary is
hard, permanent, and belongs in the database. The boundary between two
recruiters at the same agency is a product rule that will move as the product
moves, and encoding it as an RLS policy would mean a migration every time
sharing semantics change.

The cost of that choice is that this predicate lives in application code and
can be forgotten. Two things contain it: every by-id read goes through
`load_visible_opportunity`, and `tests/test_opportunity_routes_guarded.py`
asserts structurally that no route escapes it.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_message import EmailMessage
from app.models.mailbox import Mailbox
from app.models.opportunity import Opportunity
from app.models.opportunity_share import OpportunityShare

OWNER_ROLE = "owner"


def visible_opportunities(user_id: uuid.UUID, role: str) -> ColumnElement[bool]:
    """A WHERE clause, not a query — so it composes with existing sort,
    search and pagination without the caller changing shape.
    """
    if role == OWNER_ROLE:
        # A three-person agency needs the boss to see the pipeline.
        return true_()

    shared_with_me = (
        select(OpportunityShare.id)
        .where(OpportunityShare.opportunity_id == Opportunity.id)
        .where(
            or_(
                OpportunityShare.scope == OpportunityShare.SCOPE_TENANT,
                and_(
                    OpportunityShare.scope == OpportunityShare.SCOPE_USER,
                    OpportunityShare.shared_with_user_id == user_id,
                ),
            )
        )
        .exists()
    )

    # The recipient of the original mail keeps sight of what was extracted
    # from it. They have the email in Outlook; hiding the extracted version
    # of a message they can already read reads as a bug.
    mine_by_mailbox = (
        select(EmailMessage.id)
        .join(Mailbox, Mailbox.id == EmailMessage.mailbox_id)
        .where(EmailMessage.id == Opportunity.email_message_id)
        .where(Mailbox.user_id == user_id)
        .exists()
    )

    return or_(
        Opportunity.assigned_user_id.is_(None),  # the unassigned queue
        Opportunity.assigned_user_id == user_id,
        shared_with_me,
        mine_by_mailbox,
    )


def can_edit(opportunity: Opportunity, user_id: uuid.UUID, role: str) -> bool:
    """Narrower than visibility, and deliberately so.

    An unassigned job order is visible and claimable but NOT editable:
    claiming it is the act that makes it editable. Letting anyone edit a row
    nobody has taken responsibility for is the state where a wrong edit is
    least likely to be noticed.
    """
    if role == OWNER_ROLE:
        return True
    return opportunity.assigned_user_id == user_id


async def load_visible_opportunity(
    session: AsyncSession, opportunity_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> Opportunity:
    """404, never 403 — a 403 would confirm the row exists."""
    row = (
        await session.execute(
            select(Opportunity)
            .where(Opportunity.id == opportunity_id)
            .where(visible_opportunities(user_id, role))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such job order.")
    return row


async def load_editable_opportunity(
    session: AsyncSession, opportunity_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> Opportunity:
    """403 when visible but not editable.

    The opposite of the rule above, for the opposite reason: the caller can
    already see this job order, so concealing its existence would be theatre,
    and a 404 would tell a recruiter their colleague's shared job order had
    vanished.
    """
    row = await load_visible_opportunity(session, opportunity_id, user_id, role)
    if not can_edit(row, user_id, role):
        raise HTTPException(
            status_code=403, detail="This job order is shared with you, not assigned to you."
        )
    return row
```

Add the missing imports at the top: `from sqlalchemy import and_, or_, true as true_`.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_visibility.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Lint**

```bash
uv run ruff check app/services/visibility.py tests/test_visibility.py
```

Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add app/services/visibility.py tests/test_visibility.py
git commit -m "State once who may see a job order, and who may change it"
```

---

### Task 6: Make every opportunity route obey the predicate

Until this task lands, the schema exists and nothing enforces it.

**Files:**
- Modify: `backend/app/api/opportunities.py` — `list_opportunities` (l.131), `get_eligibility` (l.525), `set_review_status` (l.400), the placement-type route (l.442), the occupational-requirement route (l.480)
- Modify: `backend/app/api/auth.py` — `_require_session` (l.217) must also return the caller's role
- Create: `backend/tests/test_opportunity_routes_guarded.py`

**Interfaces:**
- Consumes: everything Task 5 produces.
- Produces: `_require_session(request) -> tuple[uuid.UUID, uuid.UUID, str]` (user, tenant, role). **This changes an existing signature used across several API modules** — every call site must be updated in this task.

- [ ] **Step 1: Write the structural guard test**

Create `backend/tests/test_opportunity_routes_guarded.py`:

```python
"""The filter lives in application code, so a test asserts nobody forgets it.

This is deliberately structural rather than behavioural: a behavioural test
covers the routes that exist today, and the failure mode being guarded
against is a route added next month.
"""

import ast
import pathlib

MODULE = pathlib.Path(__file__).parent.parent / "app" / "api" / "opportunities.py"

READ_GUARD = "load_visible_opportunity"
EDIT_GUARD = "load_editable_opportunity"

# Routes that legitimately do not load a single opportunity by id.
EXEMPT = {"list_opportunities"}


def _routes() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(MODULE.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr in {
                "get", "post", "put", "patch", "delete"
            }:
                out.append(node)
    return out


def _calls(node: ast.AsyncFunctionDef) -> set[str]:
    return {
        c.func.id
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }


def _takes_opportunity_id(node: ast.AsyncFunctionDef) -> bool:
    return any(a.arg == "opportunity_id" for a in node.args.args)


def test_every_by_id_route_loads_through_the_guard() -> None:
    offenders = [
        node.name
        for node in _routes()
        if _takes_opportunity_id(node)
        and node.name not in EXEMPT
        and not ({READ_GUARD, EDIT_GUARD} & _calls(node))
    ]
    assert offenders == [], (
        f"These routes read an opportunity by id without the visibility guard: {offenders}"
    )


def test_every_mutating_by_id_route_uses_the_edit_guard() -> None:
    mutating = []
    for node in _routes():
        if not _takes_opportunity_id(node) or node.name in EXEMPT:
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr in {
                "post", "put", "patch", "delete"
            }:
                mutating.append(node)
    offenders = [n.name for n in mutating if EDIT_GUARD not in _calls(n)]
    assert offenders == [], (
        f"These routes change an opportunity without checking edit rights: {offenders}"
    )


def test_list_filters_by_the_predicate() -> None:
    source = MODULE.read_text()
    start = source.index("async def list_opportunities")
    end = source.index("\n@router.", start)
    assert "visible_opportunities(" in source[start:end], (
        "list_opportunities does not apply the visibility predicate"
    )
```

**Note on `test_every_mutating_by_id_route_uses_the_edit_guard`:** the claim/assign routes from Task 8 are mutating and by-id but deliberately do **not** use `load_editable_opportunity` (claiming an unassigned job order is precisely the case `can_edit` refuses). Task 8 adds them to `EXEMPT` with a comment; do not weaken the test.

- [ ] **Step 2: Run and watch it fail**

```bash
uv run pytest tests/test_opportunity_routes_guarded.py -v
```

Expected: 3 failed — every by-id route is currently an offender.

- [ ] **Step 3: Make `_require_session` return the role**

In `backend/app/api/auth.py`, change `_require_session` (l.217) to look the role up and return a 3-tuple. The session cookie carries only `uid` and `tid`, so the role comes from the database:

```python
async def _require_session(request: Request) -> tuple[uuid.UUID, uuid.UUID, str]:
    """The signed-in user, their agency, and their role.

    The role is read rather than carried in the cookie: a cookie minted
    before a promotion would otherwise keep the old role until the user
    signed out, and the role now gates who can see the whole pipeline.
    """
    user_uuid, tenant_uuid = _session_identity(request)  # the existing body
    async with tenant_session(tenant_uuid) as session:
        role = (
            await session.execute(select(User.role).where(User.id == user_uuid))
        ).scalar_one_or_none()
    return user_uuid, tenant_uuid, role or "recruiter"
```

Keep the existing synchronous cookie-parsing logic as `_session_identity`. Then update **every** call site — grep for it:

```bash
grep -rn "_require_session(request)" app/
```

Each becomes `user_uuid, tenant_uuid, role = await _require_session(request)`. Call sites that ignore the role use `_user_uuid, tenant_uuid, _role`.

- [ ] **Step 4: Apply the predicate in `list_opportunities`**

In `backend/app/api/opportunities.py:131`, change the signature line and add the clause to both the count query and the page query:

```python
    user_uuid, tenant_uuid, role = await _require_session(request)
    ...
    visible = visible_opportunities(user_uuid, role)

    async with tenant_session(tenant_uuid) as session:
        for stored, n in await session.execute(
            select(Opportunity.review_status, func.count())
            .where(visible)
            .group_by(Opportunity.review_status)
        ):
```

Apply `.where(visible)` to the page-selection query as well. **The counts must use the same clause as the page** — a tab count that includes invisible rows tells a recruiter there are twelve job orders and then shows them four.

- [ ] **Step 5: Guard the four by-id routes**

For `get_eligibility` (l.525), read-only:

```python
    user_uuid, tenant_uuid, role = await _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        await load_visible_opportunity(session, opportunity_id, user_uuid, role)
```

For `set_review_status` (l.400), the placement-type route (l.442) and the occupational-requirement route (l.480), use the edit guard. `set_review_status` currently carries the comment "No ownership check: RLS policy carries both USING and WITH CHECK" — that reasoning was true when everyone in a tenant could see everything and is now wrong. Replace it:

```python
@router.post("/opportunities/{opportunity_id}/review")
async def set_review_status(
    opportunity_id: uuid.UUID, body: ReviewRequest, request: Request
) -> dict:
    """Mark a vacancy reviewed, or put it back."""
    user_uuid, tenant_uuid, role = await _require_session(request)
    new_status = _REVIEWED if body.reviewed else _READY

    async with tenant_session(tenant_uuid) as session:
        # RLS keeps this inside the agency; it says nothing about which
        # recruiter inside that agency may change the row, which is what
        # `load_editable_opportunity` decides.
        await load_editable_opportunity(session, opportunity_id, user_uuid, role)
        updated = (
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == opportunity_id)
                .values(review_status=new_status)
                .returning(Opportunity.review_status)
            )
        ).scalar_one_or_none()

    if updated is None:
        raise HTTPException(status_code=404, detail="No such job order.")

    return {
        "id": str(opportunity_id),
        "review_status": _STORED_TO_FILTER.get(updated, updated),
    }
```

The placement-type and occupational-requirement routes matter most: both write an audited human judgement (`placement_type_set_by`, `sex_requirement_set_by`) that unlocks a lawful sex filter. Letting someone edit those on a job order merely shared with them puts a name against a regulatory decision that person was never given.

- [ ] **Step 6: Run the guard test and the full suite**

```bash
uv run pytest tests/test_opportunity_routes_guarded.py -v && uv run pytest -q
```

Expected: guard test 3 passed; full suite passes. Existing opportunity API tests will need their fixtures to assign the job order to the signed-in user — that is the change in behaviour this task introduces, and updating those fixtures is part of this task.

- [ ] **Step 7: Commit**

```bash
git add app/api/ tests/
git commit -m "Stop showing every recruiter every job order"
```

---

### Task 7: Sharing endpoints

**Files:**
- Create: `backend/app/api/opportunity_shares.py`
- Modify: `backend/app/main.py` (register the router)
- Create: `backend/tests/test_opportunity_shares_api.py`

**Interfaces:**
- Consumes: `OpportunityShare` (Task 4), `load_visible_opportunity`, `can_edit`, `OWNER_ROLE` (Task 5).
- Produces: `POST/GET /api/opportunities/{id}/shares`, `DELETE /api/opportunities/{id}/shares/{share_id}`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_opportunity_shares_api.py` covering, each as its own test:

1. The assignee shares to a named colleague; the colleague can then `GET /api/opportunities/{id}` (200) but gets 403 on `/review`.
2. Re-sharing to the same colleague returns 200 and updates the note — not 409.
3. A share recipient may share onward to a third user (200).
4. A share recipient is refused `scope="tenant"` (403).
5. The assignee may broadcast `scope="tenant"` (201), and a user created afterwards sees the job order.
6. A recipient may `DELETE` their own share; an unrelated user may not (404, because they cannot see the job order).
7. Sharing a job order the caller cannot see returns 404.

Use the `sign_in` cookie helper already in `tests/test_opportunities_api.py`:

```python
def sign_in(client: httpx.AsyncClient, user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """The cookie the OAuth callback would have set, without the OAuth."""
    client.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )
```

- [ ] **Step 2: Run and watch them fail**

```bash
uv run pytest tests/test_opportunity_shares_api.py -v
```

Expected: all fail with 404 — the routes do not exist.

- [ ] **Step 3: Write the module**

Create `backend/app/api/opportunity_shares.py`:

```python
"""Showing a job order to a colleague.

A share grants sight and nothing else. The recipient may read the job order
and may pass it on to another named colleague — that is how work finds the
right person through a chain — but may not edit it, and may not throw it open
to the whole agency, because that is not theirs to decide about someone
else's client.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.auth import _require_session
from app.db.rls import tenant_session
from app.models.opportunity_share import OpportunityShare
from app.services.visibility import OWNER_ROLE, can_edit, load_visible_opportunity

router = APIRouter(tags=["opportunity-shares"])


class ShareRequest(BaseModel):
    scope: str = Field(pattern="^(user|tenant)$")
    user_ids: list[uuid.UUID] = Field(default_factory=list)
    note: str | None = None


@router.post("/opportunities/{opportunity_id}/shares", status_code=201)
async def share_opportunity(
    opportunity_id: uuid.UUID, body: ShareRequest, request: Request
) -> dict:
    user_uuid, tenant_uuid, role = await _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        # Seeing it is the right to pass it on. 404 if not.
        opportunity = await load_visible_opportunity(
            session, opportunity_id, user_uuid, role
        )

        if body.scope == OpportunityShare.SCOPE_TENANT:
            # A recipient throwing someone else's client work open to the
            # whole office is not their decision.
            if not can_edit(opportunity, user_uuid, role) and role != OWNER_ROLE:
                raise HTTPException(
                    status_code=403,
                    detail="Only the assigned recruiter can share this with the whole agency.",
                )
            statement = (
                pg_insert(OpportunityShare)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_uuid,
                    opportunity_id=opportunity_id,
                    scope=OpportunityShare.SCOPE_TENANT,
                    shared_with_user_id=None,
                    shared_by_user_id=user_uuid,
                    note=body.note,
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "opportunity_id"],
                    index_where=OpportunityShare.scope == OpportunityShare.SCOPE_TENANT,
                    set_={"note": body.note, "shared_by_user_id": user_uuid},
                )
            )
            await session.execute(statement)
            created = [None]
        else:
            if not body.user_ids:
                raise HTTPException(
                    status_code=422, detail="Name at least one colleague to share with."
                )
            created = []
            for target in body.user_ids:
                if target == user_uuid:
                    continue  # sharing with yourself is a no-op, not an error
                await session.execute(
                    pg_insert(OpportunityShare)
                    .values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_uuid,
                        opportunity_id=opportunity_id,
                        scope=OpportunityShare.SCOPE_USER,
                        shared_with_user_id=target,
                        shared_by_user_id=user_uuid,
                        note=body.note,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            "tenant_id", "opportunity_id", "shared_with_user_id"
                        ],
                        index_where=OpportunityShare.scope == OpportunityShare.SCOPE_USER,
                        set_={"note": body.note, "shared_by_user_id": user_uuid},
                    )
                )
                created.append(target)

    return {"opportunity_id": str(opportunity_id), "shared_with": len(created)}
```

Plus `GET .../shares` (visible-only, returns scope, recipient, sharer, note, created_at) and `DELETE .../shares/{share_id}` permitting the assignee, the original sharer, the owner, and a recipient removing themselves.

Register the router in `backend/app/main.py` beside the existing ones, under the same `/api` prefix.

- [ ] **Step 4: Emit the share event**

After the shares are written, emit one event carrying the new recipients. Task 10 defines `EVENT_OPPORTUNITY_SHARED` and `recipient_user_ids`; until then, leave a call that Task 10 fills in — **no**, that is a placeholder. Instead, **do Task 10 before this step** if executing out of order; when executing in order, this step is the one line:

```python
    await emit(
        OpportunityEvent(
            kind=EVENT_OPPORTUNITY_SHARED,
            tenant_id=tenant_uuid,
            opportunity_id=opportunity_id,
            recipient_user_ids=tuple(t for t in created if t is not None),
            job_title=opportunity.job_title_raw,
            company_name=opportunity.company_name_raw,
            location=opportunity.location_raw,
            salary=opportunity.salary_raw,
        )
    )
```

For a tenant broadcast, `recipient_user_ids` is `None` — which is exactly the "everyone" meaning Task 10 gives it.

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/test_opportunity_shares_api.py -v
```

Expected: 7 passed.

- [ ] **Step 6: Confirm no route escaped `/api`**

```bash
uv run pytest tests/test_routing.py -v
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add app/api/ tests/
git commit -m "Let a recruiter pass a job order to whoever can fill it"
```

---

### Task 8: Claim, assign, and create by hand

**Files:**
- Modify: `backend/app/api/opportunities.py` (three new routes)
- Modify: `backend/tests/test_opportunity_routes_guarded.py` (`EXEMPT`)
- Create: `backend/tests/test_opportunity_claim.py`

**Interfaces:**
- Consumes: Task 5's guards, Task 3's columns.
- Produces: `POST /api/opportunities/{id}/claim`, `POST /api/opportunities/{id}/assign`, `POST /api/opportunities`.

- [ ] **Step 1: Write the failing test, including the race**

Create `backend/tests/test_opportunity_claim.py`. The important one:

```python
async def test_two_recruiters_claiming_at_once_yields_one_winner() -> None:
    """A real race at 9pm, when the job orders arrive and everyone is looking."""
    tenant_id, first = await seed_tenant_with_user()
    second = await _second_user(tenant_id)
    opportunity_id = await _opportunity(tenant_id, assigned_user_id=None)
    try:
        results = await asyncio.gather(
            _claim(tenant_id, first, opportunity_id),
            _claim(tenant_id, second, opportunity_id),
        )
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 409]
    finally:
        await cleanup_tenant(tenant_id)
```

Plus: claiming an already-assigned job order is 409; `assign` with `null` releases it to the queue; a non-owner cannot assign someone else's job order to a third party; a manual create returns `source="manual"` and assigns to the creator.

- [ ] **Step 2: Run and watch it fail**

```bash
uv run pytest tests/test_opportunity_claim.py -v
```

Expected: fail — routes missing.

- [ ] **Step 3: Write the claim route with an atomic guard**

In `backend/app/api/opportunities.py`:

```python
@router.post("/opportunities/{opportunity_id}/claim")
async def claim_opportunity(opportunity_id: uuid.UUID, request: Request) -> dict:
    """Take an unassigned job order.

    The `WHERE assigned_user_id IS NULL` is the whole concurrency story: two
    recruiters pressing this at the same moment both run the same UPDATE, and
    exactly one of them matches a row.
    """
    user_uuid, tenant_uuid, role = await _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        # Visible, so an invisible id is 404 rather than 409.
        await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        claimed = (
            await session.execute(
                update(Opportunity)
                .where(Opportunity.id == opportunity_id)
                .where(Opportunity.assigned_user_id.is_(None))
                .values(assigned_user_id=user_uuid)
                .returning(Opportunity.id)
            )
        ).scalar_one_or_none()

    if claimed is None:
        raise HTTPException(status_code=409, detail="Someone else has taken this job order.")

    return {"id": str(opportunity_id), "assigned_user_id": str(user_uuid)}
```

The `assign` route takes `{"user_id": <uuid>|null}`, requires `can_edit`, and on release (`null`) emits `EVENT_OPPORTUNITY_NEW` with `recipient_user_ids=None` — a released job order is queue work again and nobody would otherwise learn it is available.

The manual-create route sets `source=Opportunity.MANUAL`, `email_message_id=None`, `assigned_user_id=user_uuid` (you typed it in, it is yours — not the client's assignee's), a plain `uuid.uuid4()` id, and `client_id` from the body, which may be `None`.

- [ ] **Step 4: Exempt claim and assign from the edit-guard test**

In `backend/tests/test_opportunity_routes_guarded.py`:

```python
# Routes that legitimately do not load a single opportunity by id, or that
# deliberately bypass `can_edit`: claiming an UNASSIGNED job order is exactly
# the case `can_edit` refuses, and `assign` is guarded by its own rule.
EXEMPT = {"list_opportunities", "claim_opportunity", "assign_opportunity"}
```

Do not weaken the assertions themselves.

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/test_opportunity_claim.py tests/test_opportunity_routes_guarded.py -v
```

Expected: all pass.

- [ ] **Step 6: Check the file size**

```bash
wc -l app/api/opportunities.py
```

Expected: under 1500. It began at 691; if this task pushes it past ~1100, split the assignment routes into `app/api/opportunity_assignment.py` before committing.

- [ ] **Step 7: Commit**

```bash
git add app/api/opportunities.py tests/
git commit -m "Let a recruiter take work off the queue, and type one in by hand"
```

---

### Task 9: Client assignee and collaborator endpoints

**Files:**
- Modify: `backend/app/api/clients.py` (867 LOC — watch the ceiling) or create `backend/app/api/client_assignment.py` if it would exceed ~1100
- Create: `backend/tests/test_client_assignment_api.py`

**Interfaces:**
- Consumes: `Client.assigned_user_id`, `ClientCollaborator` (Task 2); `Opportunity.assigned_user_id`, `Opportunity.client_id` (Task 3).
- Produces: `PUT /api/clients/{id}/assignee`, `POST`/`DELETE /api/clients/{id}/collaborators`.

- [ ] **Step 1: Write the failing tests**

Cover: assigning a client returns 200; assigning with `move_open_opportunities` (defaulting to `true`) moves that client's job orders from the outgoing recruiter to the incoming one and reports the count; with `false` it moves none; a job order for a *different* client is untouched; adding the same collaborator twice is idempotent.

- [ ] **Step 2: Run and watch them fail**

```bash
uv run pytest tests/test_client_assignment_api.py -v
```

- [ ] **Step 3: Write the endpoint**

```python
class AssigneeRequest(BaseModel):
    user_id: uuid.UUID | None
    # Defaults to true: a client changing hands normally means the work
    # changes hands. It stays a choice rather than an automatic cascade,
    # because the outgoing recruiter may be mid-placement on one of them.
    move_open_opportunities: bool = True


@router.put("/clients/{client_id}/assignee")
async def set_client_assignee(
    client_id: uuid.UUID, body: AssigneeRequest, request: Request
) -> dict:
    _user_uuid, tenant_uuid, _role = await _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        previous = (
            await session.execute(
                select(Client.assigned_user_id).where(Client.id == client_id)
            )
        ).scalar_one_or_none()
        updated = (
            await session.execute(
                update(Client)
                .where(Client.id == client_id)
                .values(assigned_user_id=body.user_id)
                .returning(Client.id)
            )
        ).scalar_one_or_none()
        if updated is None:
            raise HTTPException(status_code=404, detail="No such client.")

        moved = 0
        if body.move_open_opportunities:
            # "Open" has no schema meaning: `Opportunity` carries only
            # `review_status` and `quality_state`, neither of which expresses
            # filled, closed or lost. So every job order currently assigned to
            # the outgoing recruiter moves, and the word "open" is avoided in
            # the API. When a lifecycle state lands, this predicate narrows.
            result = await session.execute(
                update(Opportunity)
                .where(Opportunity.client_id == client_id)
                .where(Opportunity.assigned_user_id.is_not_distinct_from(previous))
                .values(assigned_user_id=body.user_id)
                .returning(Opportunity.id)
            )
            moved = len(result.scalars().all())

    return {
        "client_id": str(client_id),
        "assigned_user_id": str(body.user_id) if body.user_id else None,
        "opportunities_moved": moved,
    }
```

The response reports `opportunities_moved` so the interface can say "12 job orders moved to Sarah" rather than reassigning them quietly.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_client_assignment_api.py -v
```

- [ ] **Step 5: Check the file size**

```bash
wc -l app/api/clients.py
```

Expected: under 1500.

- [ ] **Step 6: Commit**

```bash
git add app/api/ tests/
git commit -m "Move a client's work when the client changes hands"
```

---

### Task 10: Assign at ingestion, and notify only the people concerned

**Files:**
- Modify: `backend/app/services/client_matching.py` (return the assignee alongside the id)
- Modify: `backend/app/services/ingest/persist.py:302-318`
- Modify: `backend/app/services/notify/events.py`
- Modify: `backend/app/services/notify/dispatch.py`
- Create: `backend/tests/test_ingest_assignment.py`
- Create: `backend/tests/test_notify_recipients.py`

**Interfaces:**
- Consumes: Task 3's columns, Task 2's `Client.assigned_user_id`.
- Produces: `EVENT_OPPORTUNITY_SHARED = "opportunity.shared"`, `EVENT_OPPORTUNITY_ASSIGNED = "opportunity.assigned"`, and `OpportunityEvent.recipient_user_ids: tuple[uuid.UUID, ...] | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ingest_assignment.py`:

```python
async def test_ingested_job_order_goes_to_the_clients_recruiter_not_the_mailbox_owner() -> None:
    """The mailbox was the transport. The client assignment is the authority."""
```

Seed a tenant with two users; give recruiter B the client; ingest a message into recruiter A's mailbox; assert the resulting opportunity has `assigned_user_id == B` and `client_id` set.

Also: an unmatched client leaves both NULL (the queue); re-running extraction on the same message does **not** overwrite an `assigned_user_id` a recruiter has since claimed.

`tests/test_notify_recipients.py`: an event with `recipient_user_ids=(b,)` produces deliveries only for B's destinations; `recipient_user_ids=None` keeps the current tenant-wide behaviour; a tenant-scoped destination receives events regardless.

- [ ] **Step 2: Run and watch them fail**

```bash
uv run pytest tests/test_ingest_assignment.py tests/test_notify_recipients.py -v
```

- [ ] **Step 3: Return the assignee from `match_client`**

`match_client` currently returns `uuid.UUID | None` and `persist.py:302` discards it. Change the return to a small frozen dataclass so the assignee comes back without a second query:

```python
@dataclass(frozen=True)
class MatchedClient:
    client_id: uuid.UUID
    assigned_user_id: uuid.UUID | None
```

Return `None` in exactly the cases the current docstring describes — "a message can legitimately mention no company, and inventing a client for it would be exactly the fabrication the pipeline exists to avoid (§15)". Update every existing caller.

- [ ] **Step 4: Use it in `persist.py`**

The call order already holds: `match_client` runs at l.302 and the opportunity inserts follow at l.308, so no reordering is needed.

```python
    matched = await match_client(
        session, tenant_id, email_message_id, sender_email, first_company
    )

    for index, job in enumerate(response.jobs):
        opportunity_id = _opportunity_id(email_message_id, index)
        opportunity_ids.append(opportunity_id)
        await _insert_opportunity(
            session,
            tenant_id,
            email_message_id,
            opportunity_id,
            job,
            source,
            client_id=matched.client_id if matched else None,
            assigned_user_id=matched.assigned_user_id if matched else None,
        )
```

Inside `_insert_opportunity`, write `client_id` and `assigned_user_id` **on the insert path only**. `extract_email` re-runs after a crash and replay appends — which is why `client_mentions` carries its once-per-message unique constraint — so if the update path recomputed the assignee, a recruiter who had claimed a queued job order would silently lose it to a re-run.

- [ ] **Step 5: Add recipients to the event**

`backend/app/services/notify/events.py`:

```python
EVENT_OPPORTUNITY_NEW = "opportunity.new"
EVENT_OPPORTUNITY_NEEDS_REVIEW = "opportunity.needs_review"
EVENT_OPPORTUNITY_SHARED = "opportunity.shared"
EVENT_OPPORTUNITY_ASSIGNED = "opportunity.assigned"

ALL_EVENT_KINDS: tuple[str, ...] = (
    EVENT_OPPORTUNITY_NEW,
    EVENT_OPPORTUNITY_NEEDS_REVIEW,
    EVENT_OPPORTUNITY_SHARED,
    EVENT_OPPORTUNITY_ASSIGNED,
)


@dataclass(frozen=True)
class OpportunityEvent:
    """One vacancy, denormalised at emit time."""

    kind: str
    tenant_id: uuid.UUID
    opportunity_id: uuid.UUID
    job_title: str | None
    company_name: str | None
    location: str | None
    salary: str | None
    # Who should hear about this. `None` keeps the original tenant-wide
    # meaning, so nothing else in the catalogue changes: a broadcast share and
    # an unassigned job order both legitimately concern everybody.
    recipient_user_ids: tuple[uuid.UUID, ...] | None = None
    # Set on opportunity.shared and opportunity.assigned.
    actor_name: str | None = None
    note: str | None = None
```

- [ ] **Step 6: Intersect in `dispatch.py`**

Extend the `_SUBSCRIBERS` query with an optional recipient filter. A tenant-level destination (the "one shared destination" case in the notification design) has no `user_id` and must keep receiving everything:

```sql
      AND (
        :recipients::uuid[] IS NULL
        OR d.user_id IS NULL
        OR d.user_id = ANY(:recipients)
      )
```

Pass `None` when `recipient_user_ids` is `None`. A tenant broadcast is one event with N recipients, not N events, so the existing per-event hourly cap still applies per subscriber.

- [ ] **Step 7: Set recipients at the ingestion emit site**

In `persist.py`, the existing `emit(OpportunityEvent(...))` gains:

```python
                        recipient_user_ids=(
                            (matched.assigned_user_id,)
                            if matched and matched.assigned_user_id
                            else None  # unassigned: it is queue work, tell everyone
                        ),
```

- [ ] **Step 8: Run the tests, then the whole suite**

```bash
uv run pytest tests/test_ingest_assignment.py tests/test_notify_recipients.py -v && uv run pytest -q
```

Expected: all pass.

- [ ] **Step 9: Lint everything**

```bash
uv run ruff check .
```

Expected: no output.

- [ ] **Step 10: Commit**

```bash
git add app/services/ tests/
git commit -m "Route a job order to the client's recruiter, and tell only them"
```

---

## Deployment note

Nothing in this feature calls a new external system, so the per-service env-var
check in `CLAUDE.md` does not apply. The migration chain does need running
against production in order — Tasks 0 through 4 each add one revision, and Task
3's backfill reads `client_mentions`, so it must not be squashed ahead of the
column it fills.

## Self-review

**Spec coverage.** Every section of the design maps to a task: data model →
1–4, visibility → 5–6, ingestion → 10, reassignment → 9, API → 7–9,
notifications → 10, testing → distributed across all.

**Two gaps found and closed while writing:** the spec assumed `_require_session`
already returned a role (it returns a 2-tuple, so Task 6 changes that signature
and every call site), and no task existed for registering the new tables in
`_CLEANUP_STATEMENTS` (folded into Task 2, Step 2).

**One ordering hazard:** Task 7 Step 4 emits an event whose kind Task 10
defines. Executed in order this is fine; executed out of order, do Task 10
first.
