# Candidate Ownership and Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every candidate an owning recruiter, let that recruiter share to one colleague or to the whole agency, and make the resulting "two recruiters, one person" collision comprehensible instead of a constraint violation.

**Architecture:** Mirror the job order sharing model column for column — `owner_id` on the row, a `candidate_shares` table with `scope` in (`user`, `tenant`), a visibility predicate in `app/services/visibility.py` applied in application code rather than RLS, and a structural test that fails when a route forgets it. Per-tenant email/phone uniqueness is deliberately **unchanged**, so a colliding create is answered with a thin 409 plus an access-request path rather than a duplicate row.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Alembic, Postgres 16, pytest, httpx ASGI transport; Next.js static export + Vitest for the frontend.

**Governing spec:** [2026-07-31-candidate-ownership-and-sharing-design.md](../specs/2026-07-31-candidate-ownership-and-sharing-design.md). Reference implementation to mirror: [2026-07-30-job-order-assignment-and-sharing-design.md](../specs/2026-07-30-job-order-assignment-and-sharing-design.md).

## Global Constraints

- **All config from the repo-root `.env`** via `app.core.config.settings`. No literal URLs, model names or keys in source.
- **No hardcoded values.** Where this plan shows a literal (a scope name, a status), it is a class constant referenced by name, not a string repeated at call sites.
- **Routes never take a `session` parameter.** The idiom throughout this codebase is `_require_session_with_role(request)` followed by `async with tenant_session(tenant_uuid) as session:`. A `session: AsyncSession` parameter in a route signature is read by FastAPI as a request body and breaks the route. `tenant_session` is imported from `app.db.rls`.
- **Every business table carries `tenant_id`** via `TenantScoped`, and every new table gets `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` + a `tenant_isolation` policy **in the migration itself** — `verify_rls_enforced()` (`app/db/rls.py:58`, zero arguments) refuses to boot the service otherwise.
- **Every user foreign key is composite** — `(tenant_id, user_id)` → `(users.tenant_id, users.id)`. A plain `users.id` reference would let a share row reach a user in another agency.
- **Every composite `ON DELETE SET NULL` must name its column** — `SET NULL (owner_id)`, never a bare `SET NULL`. The bare form nulls `tenant_id`, which is `NOT NULL`, so deleting a recruiter fails outright. `alembic op.create_foreign_key(..., ondelete="SET NULL (owner_id)")` emits this correctly — precedent in `20260731_0900_client_assignee_column_qualified_set_null.py`.
- **Migration head is `314cc3da9ced`** (`20260730_1646_opportunity_shares.py`). Chain: `314cc3da9ced` → `a1b2c3d4e5f6` → `f3cc4b20b322`. Task 1 hangs off `314cc3da9ced`. If a migration lands while this is being built, re-point Task 1 at the real head and chain the rest unchanged.
- **Partial-index upserts need `index_elements=[...]` plus `index_where=<literal>`**, where the literal is a module-level `text("scope = 'user'")` — a bind parameter there does not match the index and the upsert silently becomes an insert. See the note at the top of `app/api/opportunity_shares.py`.
- **Single file ≤ 1500 LOC.** `app/api/candidates.py` is 1458. Nothing new goes in it except guard calls, the two collision checks and the override changes; all new routes go in new modules.
- **404 when a row is invisible, 403 when it is visible but not editable.** A 403 on an invisible row confirms it exists.
- Run everything from `backend/`: `uv run pytest`, `uv run ruff check .`, `uv run alembic upgrade head`.

---

## File Structure

**Created**

| File | Responsibility |
|---|---|
| `backend/app/models/candidate_share.py` | One grant of sight on one candidate. |
| `backend/app/models/candidate_access_request.py` | A request to be granted that sight, and its resolution. |
| `backend/app/api/candidate_shares.py` | Share + access-request routes. |
| `backend/app/api/candidate_ownership.py` | Claim, assign, and the `scope=` list filter. Separate from `candidates.py` only because that file is at 1458 of 1500 LOC. |
| `backend/app/services/notify/candidate_events.py` | `CandidateEvent` + its emit path. |
| `backend/alembic/versions/20260731_1000_candidate_owner_id.py` | `owner_id` + backfill. |
| `backend/alembic/versions/20260731_1010_candidate_shares.py` | `candidate_shares` + RLS. |
| `backend/alembic/versions/20260731_1020_candidate_access_requests.py` | `candidate_access_requests` + RLS. |
| `backend/alembic/versions/20260731_1030_candidate_override_per_user.py` | `user_id` on overrides, key widened. |
| `backend/tests/test_candidate_routes_guarded.py` | Structural guard, mirroring `test_opportunity_routes_guarded.py`. |
| `backend/tests/test_candidate_visibility.py` | The predicate, term by term. |
| `backend/tests/test_candidate_collision.py` | The 409 on create and on PATCH. |
| `backend/tests/test_candidate_shares_api.py` | Shares and access requests. |
| `backend/tests/test_candidate_ownership_api.py` | Claim, assign, scope filter. |
| `backend/tests/test_candidate_overrides_per_user.py` | Two readings of one person. |
| `backend/tests/test_candidate_import_ownership.py` | Import ownership and skipping. |
| `backend/tests/test_candidate_merge_ownership.py` | Merge and unmerge rights. |
| `frontend/app/dashboard/candidates/candidate-share.tsx` | Share dialog + access-request inbox. |

**Modified**

| File | Change |
|---|---|
| `backend/tests/conftest.py` | Task 0 — promote the shared fixtures out of one test file. |
| `backend/app/models/candidate.py` | `Candidate.owner_id`; `CandidateFieldOverride.user_id` + widened key. |
| `backend/app/services/visibility.py` | The candidate predicate and its two loaders. |
| `backend/app/services/candidate_overrides.py` | `overridden_fields` learns the two tiers. |
| `backend/app/api/candidates.py` | Guards on 12 routes; collisions on create and PATCH; per-user override upsert. |
| `backend/app/services/imports/apply.py` | Thread `uploaded_by`; own created rows; skip held rows. |
| `backend/app/services/notify/events.py`, `dispatch.py` | Six kinds; extract the dispatch protocol. |
| `backend/app/main.py` | Register the two new routers. |
| `frontend/app/dashboard/candidates/*` | Scope filter, 409 UX, share control, request inbox. |

---

## Task 0: promote the test fixtures

**Nothing else in this plan can be tested until this exists.** `client`, `seeded` and `sign_in` live inside `tests/test_opportunities_api.py`, not in `conftest.py` — `conftest.py` has only `admin_session`, `_dispose_engines`, `cleanup_tenant` and `seed_tenant_with_user`. Every API test below would fail at collection.

**Files:**
- Modify: `backend/tests/conftest.py`, `backend/tests/test_opportunities_api.py`

**Interfaces:**
- Produces, all from `conftest.py`:
  - fixture `client` → `httpx.AsyncClient`
  - `sign_in(client, user_id, tenant_id) -> None` — **synchronous**, sets the session cookie
  - fixture `seeded` → `(make_tenant, make_opportunity, make_evidence)`; `make_tenant(slug) -> (tenant_id, user_id, mailbox_id)`
  - `make_candidate(session, tenant_id, owner_id=None, **fields) -> uuid.UUID`
  - fixture `run_import` → `async (tenant_id, uploaded_by, rows) -> ImportOutcome`

- [ ] **Step 1: Move `client`, `seeded` and `sign_in` into `conftest.py`**

Cut them from `tests/test_opportunities_api.py` and paste them unchanged into `tests/conftest.py`, keeping their docstrings — both explain a real hazard (`ASGITransport` because `TestClient` drives its own event loop; the cookie the OAuth callback would have set). Delete the now-duplicate definitions from `test_opportunities_api.py`; pytest injects conftest fixtures automatically, so no import is added there.

- [ ] **Step 2: Add a candidate factory**

Append to `conftest.py`:

```python
async def make_candidate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID | None = None,
    **fields: object,
) -> uuid.UUID:
    """Insert a candidate through the ORM, not raw SQL.

    `pipeline_stage`, `record_status` and `users.role` are NOT NULL with
    PYTHON-side defaults, which a hand-written INSERT never fires. The same
    trap `make_tenant` documents.
    """
    candidate_id = uuid.uuid4()
    session.add(
        Candidate(
            id=candidate_id,
            tenant_id=tenant_id,
            full_name=fields.pop("full_name", "Wei Ming Tan"),
            owner_id=owner_id,
            **fields,
        )
    )
    await session.flush()
    return candidate_id


async def make_user(
    session: AsyncSession, tenant_id: uuid.UUID, email: str, role: str = "recruiter"
) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, tenant_id=tenant_id, email=email, role=role))
    await session.flush()
    return user_id
```

`owner_id` does not exist until Task 1, so `make_candidate` is added here without it and gains the parameter in Task 1 Step 4. Everything else in this task stands alone.

- [ ] **Step 3: Add the `run_import` fixture**

```python
@pytest.fixture
async def run_import():
    """Drive a real import end to end. There is no existing helper for this —
    the import tests build their own rows inline, which is why Task 13 needs
    one that can vary `uploaded_by`.

    `apply_import` takes the session, so the import row and the call share one
    transaction. A second session would let the import run against a
    `candidate_imports` row that had not committed.
    """
    import datetime as dt

    from app.models.candidate import CandidateImport
    from app.services.imports.apply import apply_import
    from app.services.imports.records import CandidateRecord

    async def _run(tenant_id, uploaded_by, rows):
        async with AdminSessionLocal() as session:
            import_id = uuid.uuid4()
            session.add(
                CandidateImport(
                    id=import_id,
                    tenant_id=tenant_id,
                    uploaded_by=uploaded_by,
                    # All four are NOT NULL with no default (candidate.py:707-710).
                    # The values are irrelevant to what these tests assert; their
                    # presence is not.
                    filename="test.xlsx",
                    content_type="application/vnd.ms-excel",
                    byte_size=1,
                    object_key=f"test/{import_id}",
                )
            )
            await session.flush()
            return await apply_import(
                session,
                tenant_id=tenant_id,
                import_id=import_id,
                candidates=[CandidateRecord(**row) for row in rows],
                roles=[],
                today=dt.date(2026, 7, 31),
            )

    return _run
```

`today` is passed a fixed date rather than `date.today()`: an import test whose result depends on when it runs is a test that fails one morning for no reason. Confirm `CandidateRecord`'s real module and field names before writing the call — `app/services/imports/records.py` is where the parser defines them.

- [ ] **Step 4: Confirm nothing regressed**

Run: `uv run pytest tests/test_opportunities_api.py -v`
Expected: PASS, unchanged. The fixtures moved; the tests did not.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/conftest.py backend/tests/test_opportunities_api.py
git commit -m "test: promote client, seeded and sign_in into conftest"
```

---

## Task 1: `owner_id` on candidates

**Files:**
- Modify: `backend/app/models/candidate.py`, `backend/tests/conftest.py`
- Create: `backend/alembic/versions/20260731_1000_candidate_owner_id.py`
- Test: `backend/tests/test_candidate_visibility.py`

**Interfaces:**
- Consumes: Task 0's `make_candidate`, `make_user`.
- Produces: `Candidate.owner_id: Mapped[uuid.UUID | None]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_candidate_visibility.py`:

```python
"""Who, inside one agency, may see and edit a candidate."""

import uuid

import pytest
from sqlalchemy import select, text

from app.models.candidate import Candidate
from tests.conftest import make_candidate, make_user


@pytest.mark.asyncio
async def test_candidate_has_an_owner_column(admin_session) -> None:
    row = (
        await admin_session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'candidates' AND column_name = 'owner_id'"
            )
        )
    ).first()
    assert row is not None, "candidates.owner_id does not exist"
    assert row.is_nullable == "YES", "owner_id must be nullable — NULL is the queue"


@pytest.mark.asyncio
async def test_deleting_a_recruiter_releases_their_candidates(admin_session, seeded) -> None:
    """The column-qualified SET NULL. A bare SET NULL nulls tenant_id, which is
    NOT NULL, so deleting a recruiter would fail outright."""
    make_tenant, _make_opportunity, _make_evidence = seeded
    tenant_id, recruiter, _mailbox = await make_tenant("agency-owner-release")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=recruiter)
    await admin_session.commit()

    await admin_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": recruiter})
    await admin_session.commit()

    row = (
        await admin_session.execute(
            select(Candidate.owner_id, Candidate.tenant_id).where(Candidate.id == candidate_id)
        )
    ).one()
    assert row.owner_id is None
    assert row.tenant_id == tenant_id, "tenant_id was nulled — the SET NULL is not column-qualified"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_visibility.py -v`
Expected: FAIL — `candidates.owner_id does not exist`.

- [ ] **Step 3: Add the column to the model**

In `backend/app/models/candidate.py`, inside `Candidate` beside `created_by`:

```python
    # The recruiter this candidate belongs to. NULL means the claimable queue,
    # not "hidden" — the same meaning `opportunities.assigned_user_id` carries.
    #
    # `created_by` stays what it is: an audit column recording who typed the
    # row. Ownership moves; authorship does not, and conflating them would
    # rewrite history every time a candidate changed hands.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), index=True)
```

In `__table_args__`, beside the existing `merged_into_candidate_id` foreign key:

```python
        # Composite, so a share can never reach a user in another agency.
        #
        # The column list on SET NULL is not optional: a bare SET NULL on a
        # COMPOSITE key nulls every referencing column including `tenant_id`,
        # which is NOT NULL — so deleting a recruiter would fail outright
        # rather than releasing their candidates to the queue.
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidates_owner_same_tenant",
            ondelete="SET NULL (owner_id)",
        ),
```

- [ ] **Step 4: Give `make_candidate` the parameter**

Add `owner_id=owner_id` to the `Candidate(...)` construction in `conftest.py` — it was written in Task 0 against a column that did not exist yet.

- [ ] **Step 5: Write the migration**

Create `backend/alembic/versions/20260731_1000_candidate_owner_id.py`:

```python
"""candidates.owner_id, backfilled from created_by

Revision ID: c1a0d5e7b201
Revises: 314cc3da9ced
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c1a0d5e7b201"
down_revision: str | None = "314cc3da9ced"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidates", sa.Column("owner_id", sa.UUID(), nullable=True))
    op.create_index("ix_candidates_owner_id", "candidates", ["owner_id"])
    # Alembic emits the column list correctly — precedent in
    # 20260731_0900_client_assignee_column_qualified_set_null.py, which exists
    # because `clients.assigned_user_id` shipped with the bare form.
    op.create_foreign_key(
        "fk_candidates_owner_same_tenant",
        "candidates",
        "users",
        ["tenant_id", "owner_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL (owner_id)",
    )
    # `created_by` is the closest honest answer available: every existing
    # candidate was typed by somebody. Imported and seeded rows have no
    # `created_by`, stay NULL, and land in the claimable queue.
    op.execute("UPDATE candidates SET owner_id = created_by WHERE created_by IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint("fk_candidates_owner_same_tenant", "candidates", type_="foreignkey")
    op.drop_index("ix_candidates_owner_id", table_name="candidates")
    op.drop_column("candidates", "owner_id")
```

- [ ] **Step 6: Apply it and run the tests**

Run: `uv run alembic upgrade head && uv run pytest tests/test_candidate_visibility.py -v`
Expected: both PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/candidate.py backend/tests/conftest.py backend/alembic/versions/20260731_1000_candidate_owner_id.py backend/tests/test_candidate_visibility.py
git commit -m "feat: give candidates an owning recruiter"
```

---

## Task 2: the `candidate_shares` table

**Files:**
- Create: `backend/app/models/candidate_share.py`, `backend/alembic/versions/20260731_1010_candidate_shares.py`
- Test: `backend/tests/test_candidate_shares_api.py`
- Mirror: `backend/app/models/opportunity_share.py`, `backend/alembic/versions/20260730_1646_opportunity_shares.py`

**Interfaces:**
- Produces: `CandidateShare` with `SCOPE_USER = "user"`, `SCOPE_TENANT = "tenant"`, `SCOPES`, and columns `candidate_id`, `scope`, `shared_with_user_id`, `shared_by_user_id`, `note`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_candidate_shares_api.py`:

```python
"""Sharing a candidate: the table's own guarantees, before any route exists."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.candidate_share import CandidateShare
from tests.conftest import make_candidate, make_user


@pytest.mark.asyncio
async def test_a_tenant_share_must_have_no_recipient(admin_session, seeded) -> None:
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-share-pairing")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_TENANT,
            shared_with_user_id=owner,  # illegal: a broadcast names nobody
        )
    )
    with pytest.raises(IntegrityError):
        await admin_session.flush()


@pytest.mark.asyncio
async def test_deleting_a_recipient_deletes_their_shares(admin_session, seeded) -> None:
    """CASCADE, not SET NULL. SET NULL would turn a targeted share into a
    broadcast and violate ck_candidate_shares_scope_target, making the user
    DELETE fail outright."""
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-share-cascade")
    recipient = await make_user(admin_session, tenant_id, "colleague@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=recipient,
            shared_by_user_id=owner,
        )
    )
    await admin_session.commit()

    await admin_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": recipient})
    await admin_session.commit()

    left = (
        await admin_session.execute(
            text("SELECT count(*) AS n FROM candidate_shares WHERE candidate_id = :c"),
            {"c": candidate_id},
        )
    ).one()
    assert left.n == 0


@pytest.mark.asyncio
async def test_a_share_cannot_reach_another_agency(admin_session, seeded) -> None:
    """Refused by the composite foreign key, not merely by application code."""
    make_tenant, _, _ = seeded
    tenant_a, owner_a, _ = await make_tenant("agency-a-cross")
    tenant_b, owner_b, _ = await make_tenant("agency-b-cross")
    candidate_id = await make_candidate(admin_session, tenant_a, owner_id=owner_a)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_a,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=owner_b,  # a user in the other agency
            shared_by_user_id=owner_a,
        )
    )
    with pytest.raises(IntegrityError):
        await admin_session.flush()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_shares_api.py -v`
Expected: FAIL — `ModuleNotFoundError: app.models.candidate_share`.

- [ ] **Step 3: Write the model**

Create `backend/app/models/candidate_share.py`:

```python
"""One grant of sight on one candidate.

Sharing never copies. A forked candidate would make the same person exist
twice, and every headcount, dedup and "have we approached them before" answer
would become wrong by construction — so a share is a row that says who may see
the canonical one.

There is no `access` column. Every share is read: exactly one person, the
owner, can edit a candidate, which leaves no permission lattice to reason
about. The one thing a recipient may write is an activity row, which records
what the recipient did rather than editing the candidate.

Structurally identical to `opportunity_share.py`, deliberately — one sharing
idiom in this codebase rather than two.
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


class CandidateShare(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "candidate_shares"

    SCOPE_USER = "user"
    SCOPE_TENANT = "tenant"
    SCOPES = (SCOPE_USER, SCOPE_TENANT)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
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
            "scope IN ('user', 'tenant')", name="ck_candidate_shares_scope_known"
        ),
        CheckConstraint(
            "(scope = 'tenant') = (shared_with_user_id IS NULL)",
            name="ck_candidate_shares_scope_target",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_shares_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        # CASCADE: a share to a deleted user is meaningless, and SET NULL
        # would both convert it into a tenant broadcast and violate
        # ck_candidate_shares_scope_target — making the user DELETE fail.
        ForeignKeyConstraint(
            ["tenant_id", "shared_with_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_shares_recipient_same_tenant",
            ondelete="CASCADE",
        ),
        # SET NULL, for the opposite reason: the fact that someone shared this
        # must outlive the account that did. The column list is not optional —
        # see the note in `opportunity_share.py`.
        ForeignKeyConstraint(
            ["tenant_id", "shared_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_shares_sharer_same_tenant",
            ondelete="SET NULL (shared_by_user_id)",
        ),
        Index(
            "uq_candidate_shares_per_user",
            "tenant_id",
            "candidate_id",
            "shared_with_user_id",
            unique=True,
            postgresql_where=text("scope = 'user'"),
        ),
        Index(
            "uq_candidate_shares_per_tenant",
            "tenant_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("scope = 'tenant'"),
        ),
    )
```

- [ ] **Step 4: Register the model**

`backend/app/models/__init__.py` is an explicit import list plus `__all__`, and `alembic/env.py:13` does `from app.models import *` to register every model on `Base.metadata`. Add `CandidateShare` to both.

Miss this and nothing fails today — the migration still runs. It fails later, silently and badly: the next `alembic revision --autogenerate` sees a table with no model and proposes **dropping** it.

- [ ] **Step 5: Write the migration**

Open `backend/alembic/versions/20260730_1646_opportunity_shares.py` and **copy its RLS block verbatim**, changing only the table name. Do not retype it from memory — the `tenant_isolation` policy predicate must match the one every other table uses, or `verify_rls_enforced()` will accept a policy that does not isolate.

Create `backend/alembic/versions/20260731_1010_candidate_shares.py` with `revision = "c1a0d5e7b202"`, `down_revision = "c1a0d5e7b201"`, and an `op.create_table` that reproduces the model column for column: both CHECK constraints, all three composite FKs (`create_foreign_key` handles the column-qualified `SET NULL`), the two `create_index` calls with `postgresql_where=sa.text(...)`, the two plain indexes, and the copied RLS block. `downgrade()` is `op.drop_table("candidate_shares")`.

- [ ] **Step 6: Apply and verify RLS is real**

Run: `uv run alembic upgrade head && uv run pytest tests/test_candidate_shares_api.py -v`
Expected: all three PASS.

Run: `uv run python -c "import asyncio; from app.db.rls import verify_rls_enforced; asyncio.run(verify_rls_enforced())"`
Expected: no exception. `verify_rls_enforced()` takes **no arguments** — it opens `SessionLocal` itself, as `app/main.py:60` does.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/candidate_share.py backend/app/models/__init__.py backend/alembic/versions/20260731_1010_candidate_shares.py backend/tests/test_candidate_shares_api.py
git commit -m "feat: add candidate_shares, mirroring opportunity_shares"
```

---

## Task 3: the `candidate_access_requests` table

**Files:**
- Create: `backend/app/models/candidate_access_request.py`, `backend/alembic/versions/20260731_1020_candidate_access_requests.py`
- Test: `backend/tests/test_candidate_shares_api.py` (append)

**Interfaces:**
- Produces: `CandidateAccessRequest` with `STATUS_PENDING`, `STATUS_GRANTED`, `STATUS_DECLINED`, `STATUSES`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_candidate_shares_api.py`:

```python
@pytest.mark.asyncio
async def test_only_one_pending_request_per_person(admin_session, seeded) -> None:
    """A recruiter clicking twice must not spam the owner."""
    from app.models.candidate_access_request import CandidateAccessRequest

    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-request-dedupe")
    asker = await make_user(admin_session, tenant_id, "asker@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    for _ in range(2):
        admin_session.add(
            CandidateAccessRequest(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                requested_by_user_id=asker,
                status=CandidateAccessRequest.STATUS_PENDING,
            )
        )
    with pytest.raises(IntegrityError):
        await admin_session.flush()
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_shares_api.py::test_only_one_pending_request_per_person -v`
Expected: FAIL — `ModuleNotFoundError: app.models.candidate_access_request`.

- [ ] **Step 3: Write the model**

Create `backend/app/models/candidate_access_request.py`:

```python
"""A recruiter asking to be shown a candidate a colleague holds.

This exists because a notification is not a record. A request that lives only
as a notification cannot be answered twice, cannot be listed, and cannot be
shown as pending — so the requester goes on believing it is open and asks
again.

Granting a request creates a `candidate_shares` row. The share is the grant;
this table is the record of how it came about.
"""

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class CandidateAccessRequest(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "candidate_access_requests"

    STATUS_PENDING = "pending"
    STATUS_GRANTED = "granted"
    STATUS_DECLINED = "declined"
    STATUSES = (STATUS_PENDING, STATUS_GRANTED, STATUS_DECLINED)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    note: Mapped[str | None] = mapped_column(Text)

    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'granted', 'declined')",
            name="ck_candidate_access_requests_status_known",
        ),
        # A resolution has a time, or the request is still open. Same paired-
        # nullability idiom as ck_candidate_shares_scope_target.
        CheckConstraint(
            "(status = 'pending') = (resolved_at IS NULL)",
            name="ck_candidate_access_requests_resolution_paired",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_access_requests_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "requested_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_access_requests_asker_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "resolved_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_access_requests_resolver_same_tenant",
            ondelete="SET NULL (resolved_by_user_id)",
        ),
        # One open request at a time. Resolved rows are not covered, so the
        # same person may ask again after a decline — circumstances change.
        Index(
            "uq_candidate_access_requests_one_pending",
            "tenant_id",
            "candidate_id",
            "requested_by_user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
```

- [ ] **Step 4: Write the migration**

Add `CandidateAccessRequest` to `backend/app/models/__init__.py`'s imports and `__all__`, for the reason Task 2 Step 4 gives.

Create `backend/alembic/versions/20260731_1020_candidate_access_requests.py`, `revision = "c1a0d5e7b203"`, `down_revision = "c1a0d5e7b202"`, mirroring the model column for column with both CHECKs, all three composite FKs, the partial unique index, and the same copied RLS block with the table name substituted.

- [ ] **Step 5: Apply and run**

Run: `uv run alembic upgrade head && uv run pytest tests/test_candidate_shares_api.py -v`
Expected: all four PASS.

Then confirm the model registration took, which is the check that catches a missed `__init__.py`:

Run: `uv run alembic revision --autogenerate -m "throwaway" && grep -c drop_table backend/alembic/versions/*throwaway*`
Expected: the generated file proposes **no** `drop_table`. Delete it either way — it exists only to prove the metadata is complete.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/candidate_access_request.py backend/app/models/__init__.py backend/alembic/versions/20260731_1020_candidate_access_requests.py backend/tests/test_candidate_shares_api.py
git commit -m "feat: add candidate_access_requests"
```

---

## Task 4: per-user field overrides

The spec's hardest schema change and the one most likely to go wrong quietly. `CandidateFieldOverride` is unique on `(tenant_id, candidate_id, field_name)` (`candidate.py:600`) and is **live machinery**: `PATCH /candidates/{id}` upserts it at `candidates.py:982` to mean *a human touched this field, so a later import must not overwrite it*; `app/services/candidate_overrides.py:19` reads it to render; `app/services/imports/undo.py:217` reads it too.

**Files:**
- Modify: `backend/app/models/candidate.py`, `backend/app/api/candidates.py`, `backend/app/services/candidate_overrides.py`, `backend/app/services/imports/undo.py`
- Create: `backend/alembic/versions/20260731_1030_candidate_override_per_user.py`
- Test: `backend/tests/test_candidate_overrides_per_user.py`

**Interfaces:**
- Produces: `CandidateFieldOverride.user_id: Mapped[uuid.UUID | None]`; constraint `uq_candidate_overrides_one_per_field_per_user` over `(tenant_id, candidate_id, user_id, field_name)`; `overridden_fields(session, candidate_id, user_id)` — **note the new third parameter**, which every existing caller must pass.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_candidate_overrides_per_user.py`:

```python
"""Two recruiters, one person, two readings of them.

The base row holds facts. Judgement — salary expectation, seniority,
availability — is attributed to the recruiter who formed it.
"""

import uuid

import pytest
from sqlalchemy import text

from app.models.candidate import CandidateFieldOverride
from tests.conftest import make_candidate, make_user


@pytest.mark.asyncio
async def test_two_recruiters_hold_different_values_for_one_field(
    admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, first, _ = await make_tenant("agency-two-readings")
    second = await make_user(admin_session, tenant_id, "second@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=first)

    for user_id, value in ((first, "9000"), (second, "8000")):
        admin_session.add(
            CandidateFieldOverride(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                user_id=user_id,
                field_name="salary_expectation",
                human_value=value,
                changed_by=user_id,
            )
        )
    await admin_session.flush()  # must not raise: the key includes user_id

    rows = (
        await admin_session.execute(
            text(
                "SELECT human_value FROM candidate_field_overrides "
                "WHERE candidate_id = :c ORDER BY human_value"
            ),
            {"c": candidate_id},
        )
    ).all()
    assert [r.human_value for r in rows] == ["8000", "9000"]


@pytest.mark.asyncio
async def test_the_tenant_wide_tier_survives_and_stays_singular(
    admin_session, seeded
) -> None:
    """`user_id IS NULL` is a distinct, permanent tier — not a missing value.

    Every override written before this change was import protection for the
    whole agency. Backfilling them to `changed_by` would have made them one
    person's private opinion and let the next import overwrite the field for
    everyone else.

    And a NULL does not collide with another NULL in a Postgres UNIQUE
    constraint, so without a second partial index there could be two
    agency-wide overrides on one field.
    """
    from sqlalchemy.exc import IntegrityError

    make_tenant, _, _ = seeded
    tenant_id, first, _ = await make_tenant("agency-tenant-tier")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=first)

    for _ in range(2):
        admin_session.add(
            CandidateFieldOverride(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                user_id=None,
                field_name="current_title",
                human_value="Tech Lead",
                changed_by=first,
            )
        )
    with pytest.raises(IntegrityError):
        await admin_session.flush()


@pytest.mark.asyncio
async def test_rendering_reads_the_null_tier_plus_the_callers(
    admin_session, seeded
) -> None:
    from app.services.candidate_overrides import overridden_fields

    make_tenant, _, _ = seeded
    tenant_id, first, _ = await make_tenant("agency-render-tiers")
    second = await make_user(admin_session, tenant_id, "render2@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=first)
    for user_id, field in ((None, "current_title"), (first, "salary_expectation")):
        admin_session.add(
            CandidateFieldOverride(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                user_id=user_id,
                field_name=field,
                human_value="x",
                changed_by=first,
            )
        )
    await admin_session.flush()

    assert await overridden_fields(admin_session, candidate_id, first) == {
        "current_title",
        "salary_expectation",
    }
    # The second recruiter sees the agency-wide tier and their own — not the
    # first recruiter's private reading.
    assert await overridden_fields(admin_session, candidate_id, second) == {"current_title"}
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_overrides_per_user.py -v`
Expected: FAIL — `TypeError: 'user_id' is an invalid keyword argument for CandidateFieldOverride`.

- [ ] **Step 3: Change the model**

In `CandidateFieldOverride`, beside `changed_by`:

```python
    # Whose reading this is. NULL is a distinct, permanent tier meaning
    # "agency-wide import protection" — the meaning every row written before
    # candidates had owners carries, and the meaning a shared base fact keeps.
    #
    # `changed_by` is not this column and cannot be: it is a nullable SET NULL
    # audit trail, so it empties when the account is deleted, and an identity
    # key that vanishes is not an identity key.
    user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), index=True)
```

Replace the `UniqueConstraint` and add two things beside it:

```python
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "user_id",
            "field_name",
            name="uq_candidate_overrides_one_per_field_per_user",
        ),
        # A NULL does not collide with another NULL in a Postgres UNIQUE
        # constraint, so the constraint above does not bound the tenant-wide
        # tier. This does.
        Index(
            "uq_candidate_overrides_one_tenant_wide_per_field",
            "tenant_id",
            "candidate_id",
            "field_name",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
        # CASCADE, not SET NULL: a departed recruiter's private opinion must
        # not silently become agency-wide import protection, which is exactly
        # what SET NULL would do here.
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_overrides_user_same_tenant",
            ondelete="CASCADE",
        ),
```

Postgres 15+ offers `UNIQUE NULLS NOT DISTINCT` as an alternative to the partial index. It is not used here: the partial index states the tenant-wide tier explicitly, and the two rules — "one per user per field" and "one agency-wide per field" — read as two rules because they are two.

- [ ] **Step 4: Write the migration**

Create `backend/alembic/versions/20260731_1030_candidate_override_per_user.py`:

```python
"""candidate_field_overrides gains user_id; existing rows stay tenant-wide

Revision ID: c1a0d5e7b204
Revises: c1a0d5e7b203
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c1a0d5e7b204"
down_revision: str | None = "c1a0d5e7b203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidate_field_overrides", sa.Column("user_id", sa.UUID(), nullable=True))
    op.create_index(
        "ix_candidate_field_overrides_user_id", "candidate_field_overrides", ["user_id"]
    )
    op.create_foreign_key(
        "fk_candidate_overrides_user_same_tenant",
        "candidate_field_overrides",
        "users",
        ["tenant_id", "user_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    # Existing rows keep user_id NULL on purpose. They were written to protect
    # a field from a later import for the whole agency, not to express one
    # recruiter's view, and backfilling them to `changed_by` would quietly
    # convert agency-wide protection into private opinion.
    op.drop_constraint(
        "uq_candidate_overrides_one_per_field", "candidate_field_overrides", type_="unique"
    )
    op.create_unique_constraint(
        "uq_candidate_overrides_one_per_field_per_user",
        "candidate_field_overrides",
        ["tenant_id", "candidate_id", "user_id", "field_name"],
    )
    op.create_index(
        "uq_candidate_overrides_one_tenant_wide_per_field",
        "candidate_field_overrides",
        ["tenant_id", "candidate_id", "field_name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_candidate_overrides_one_tenant_wide_per_field",
        table_name="candidate_field_overrides",
    )
    op.drop_constraint(
        "uq_candidate_overrides_one_per_field_per_user",
        "candidate_field_overrides",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_candidate_overrides_one_per_field",
        "candidate_field_overrides",
        ["tenant_id", "candidate_id", "field_name"],
    )
    op.drop_constraint(
        "fk_candidate_overrides_user_same_tenant", "candidate_field_overrides", type_="foreignkey"
    )
    op.drop_index("ix_candidate_field_overrides_user_id", table_name="candidate_field_overrides")
    op.drop_column("candidate_field_overrides", "user_id")
```

- [ ] **Step 5: Fix the three call sites**

**Write** — `app/api/candidates.py:982`. The old constraint name no longer exists, so the PATCH route raises at runtime until this changes:

```python
        stmt = (
            pg_insert(CandidateFieldOverride)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                candidate_id=candidate_uuid,
                user_id=user_uuid,
                field_name=field_name,
                human_value=human_value,
                changed_by=user_uuid,
            )
            .on_conflict_do_update(
                constraint="uq_candidate_overrides_one_per_field_per_user",
                set_={"human_value": human_value, "changed_by": user_uuid},
            )
        )
```

**Render** — `app/services/candidate_overrides.py:19`. `overridden_fields` currently selects with no user filter. Give it the caller and both tiers:

```python
async def overridden_fields(
    session: AsyncSession, candidate_id: uuid.UUID, user_id: uuid.UUID | None
) -> set[str]:
    """Which fields carry a human value, for THIS reader.

    Two tiers, ORed: `user_id IS NULL` is agency-wide — a fact somebody
    corrected for everybody, and the meaning every row written before
    candidates had owners carries. A row naming a user is that recruiter's own
    reading of a judgement field, and nobody else's business.
    """
    rows = await session.execute(
        select(CandidateFieldOverride.field_name)
        .where(CandidateFieldOverride.candidate_id == candidate_id)
        .where(
            or_(
                CandidateFieldOverride.user_id.is_(None),
                CandidateFieldOverride.user_id == user_id,
            )
        )
    )
    return set(rows.scalars().all())
```

Every existing caller of `overridden_fields` must now pass the signed-in user. Find them with `grep -rn overridden_fields app/` and update each; a caller that has no user in scope is a caller that was rendering somebody else's view and needs one threaded in.

**Import protection** — `app/services/imports/undo.py:217`. An import must respect the agency-wide tier plus the row owner's tier: `.where(or_(CandidateFieldOverride.user_id.is_(None), CandidateFieldOverride.user_id == candidate.owner_id))`.

- [ ] **Step 6: Draw the fact/judgement line, and make it enforceable**

The spec requires the field-by-field split and gives the rule: **fact stays shared, judgement goes per-user.** Identity (`full_name`, `email`, `phone_raw`, `phone_e164`), documents and activity history are facts. Assessment fields are judgement.

Do not hand-maintain that as a list in prose — a column added next month would land in neither category silently. Put it in `app/services/candidate_overrides.py` as two explicit frozensets and add a test that fails when a `Candidate` column appears in neither:

```python
def test_every_candidate_column_is_classified() -> None:
    """A new column must be declared fact or judgement, deliberately.

    The failure mode this prevents is silent: an unclassified field defaults
    to whichever branch the code happens to take, and nobody finds out until
    two recruiters disagree about it in production.
    """
    from app.models.candidate import Candidate
    from app.services.candidate_overrides import JUDGEMENT_FIELDS, SHARED_FACT_FIELDS

    columns = {c.name for c in Candidate.__table__.columns} - {
        "id", "tenant_id", "created_at", "updated_at", "created_by",
        "updated_by", "owner_id", "import_id", "merged_into_candidate_id",
        "record_status", "pipeline_stage",
    }
    unclassified = columns - JUDGEMENT_FIELDS - SHARED_FACT_FIELDS
    assert unclassified == set(), f"classify these as fact or judgement: {unclassified}"
```

Populate the two sets from the real column list the test prints on its first run. Where a field is genuinely arguable, put it in `SHARED_FACT_FIELDS` — a shared value that turns out to need splitting is a smaller mistake than a private value nobody else can see.

- [ ] **Step 7: Run the whole suite**

Run: `uv run alembic upgrade head && uv run pytest -v`
Expected: PASS, including the pre-existing override and import-undo tests. If an existing test asserts the old constraint name, update the assertion — the rename is intended.

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/candidate.py backend/app/api/candidates.py backend/app/services/candidate_overrides.py backend/app/services/imports/undo.py backend/alembic/versions/20260731_1030_candidate_override_per_user.py backend/tests/test_candidate_overrides_per_user.py
git commit -m "feat: attribute candidate field overrides to a recruiter"
```

---

## Task 5: the visibility predicate

**Files:**
- Modify: `backend/app/services/visibility.py`
- Test: `backend/tests/test_candidate_visibility.py` (append)

**Interfaces:**
- Produces:
  - `candidate_shared_with_me_exists(user_id: uuid.UUID) -> ColumnElement[bool]`
  - `visible_candidates(user_id: uuid.UUID, role: str) -> ColumnElement[bool]`
  - `can_edit_candidate(candidate: Candidate, user_id: uuid.UUID, role: str) -> bool`
  - `async load_visible_candidate(session, candidate_id, user_id, role) -> Candidate`
  - `async load_editable_candidate(session, candidate_id, user_id, role) -> Candidate`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_candidate_visibility.py`:

```python
from app.models.candidate_share import CandidateShare
from app.services.visibility import can_edit_candidate, visible_candidates


@pytest.mark.asyncio
async def test_predicate_terms(admin_session, seeded) -> None:
    """Each term, one at a time. A leak is usually one term too wide."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-predicate")
    colleague = await make_user(admin_session, tenant_id, "colleague@agency.test")

    mine = await make_candidate(admin_session, tenant_id, owner_id=me)
    theirs = await make_candidate(admin_session, tenant_id, owner_id=colleague)
    queued = await make_candidate(admin_session, tenant_id, owner_id=None)
    shared = await make_candidate(admin_session, tenant_id, owner_id=colleague)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=shared,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=me,
            shared_by_user_id=colleague,
        )
    )
    await admin_session.flush()

    visible = set(
        (
            await admin_session.execute(
                select(Candidate.id).where(visible_candidates(me, "recruiter"))
            )
        )
        .scalars()
        .all()
    )
    assert mine in visible
    assert queued in visible, "the unclaimed queue must be conspicuous"
    assert shared in visible
    assert theirs not in visible, "a colleague's private candidate leaked"

    everything = set(
        (
            await admin_session.execute(
                select(Candidate.id).where(visible_candidates(me, "owner"))
            )
        )
        .scalars()
        .all()
    )
    assert theirs in everything, "role=owner must see the whole database"


def test_an_unowned_candidate_is_visible_but_not_editable() -> None:
    """Claiming is the act that creates edit rights."""
    unowned = Candidate(id=uuid.uuid4(), full_name="Wei Ming", owner_id=None)
    assert can_edit_candidate(unowned, uuid.uuid4(), "recruiter") is False
    assert can_edit_candidate(unowned, uuid.uuid4(), "owner") is True
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_visibility.py -v`
Expected: FAIL — `ImportError: cannot import name 'visible_candidates'`.

- [ ] **Step 3: Add the predicate**

Append to `backend/app/services/visibility.py` and extend its module docstring to say it now covers candidates too:

```python
def candidate_shared_with_me_exists(user_id: uuid.UUID) -> ColumnElement[bool]:
    """A share that reaches `user_id` — a named share or a tenant broadcast.

    The single source of truth for "shared with me" on candidates, for the
    same reason the opportunity version is: the predicate, the list payload's
    row badge and the `scope=shared_with_me` filter all call this, so none of
    them can drift from the others.
    """
    return (
        select(CandidateShare.id)
        .where(CandidateShare.candidate_id == Candidate.id)
        .where(
            or_(
                CandidateShare.scope == CandidateShare.SCOPE_TENANT,
                and_(
                    CandidateShare.scope == CandidateShare.SCOPE_USER,
                    CandidateShare.shared_with_user_id == user_id,
                ),
            )
        )
        .exists()
    )


def visible_candidates(user_id: uuid.UUID, role: str) -> ColumnElement[bool]:
    """A WHERE clause, not a query.

    There is no mailbox term, unlike `visible_opportunities`. Candidates never
    arrive from the email pipeline, so no recipient has a prior claim on one.
    """
    if role == OWNER_ROLE:
        return true_()

    return or_(
        Candidate.owner_id.is_(None),  # the unclaimed queue
        Candidate.owner_id == user_id,
        candidate_shared_with_me_exists(user_id),
    )


def can_edit_candidate(candidate: Candidate, user_id: uuid.UUID, role: str) -> bool:
    """An unowned candidate is visible and claimable but NOT editable.

    Claiming it is the act that makes it editable. A row nobody has taken
    responsibility for is where a wrong edit is least likely to be noticed.
    """
    if role == OWNER_ROLE:
        return True
    return candidate.owner_id == user_id


async def load_visible_candidate(
    session: AsyncSession, candidate_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> Candidate:
    """404, never 403 — a 403 would confirm the row exists."""
    row = (
        await session.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .where(visible_candidates(user_id, role))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such candidate.")
    return row


async def load_editable_candidate(
    session: AsyncSession, candidate_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> Candidate:
    """403 when visible but not editable.

    The caller can already see this candidate, so concealing its existence
    would be theatre, and a 404 would tell a recruiter that a colleague's
    shared candidate had vanished.
    """
    row = await load_visible_candidate(session, candidate_id, user_id, role)
    if not can_edit_candidate(row, user_id, role):
        raise HTTPException(
            status_code=403,
            detail="This candidate is shared with you, not assigned to you.",
        )
    return row
```

Add to the imports:

```python
from app.models.candidate import Candidate
from app.models.candidate_share import CandidateShare
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_candidate_visibility.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/visibility.py backend/tests/test_candidate_visibility.py
git commit -m "feat: add the candidate visibility predicate"
```

---

## Task 6: candidate events

`emit_and_enqueue` takes an `OpportunityEvent` — a frozen dataclass carrying `job_title`, `company_name`, `location`, `salary`, which feed the WhatsApp templates. There is no generic emit, so "copy the pattern" is not enough: a candidate event needs a type of its own.

**Files:**
- Create: `backend/app/services/notify/candidate_events.py`
- Modify: `backend/app/services/notify/events.py`, `backend/app/services/notify/dispatch.py`
- Test: `backend/tests/test_candidate_notifications.py`

**Interfaces:**
- Produces: `CandidateEvent(kind, tenant_id, candidate_id, candidate_name, recipient_user_ids=None, actor_name=None, note=None)` and `async emit_candidate_event(event: CandidateEvent) -> int`; the six kind constants.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_candidate_notifications.py`:

```python
"""A tenant broadcast is one event with N recipients, not N events."""

import pytest

from app.services.notify import events


def test_the_six_candidate_kinds_exist() -> None:
    assert events.CANDIDATE_SHARED == "candidate.shared"
    assert events.CANDIDATE_ASSIGNED == "candidate.assigned"
    assert events.CANDIDATE_UNCLAIMED == "candidate.unclaimed"
    assert events.CANDIDATE_ACCESS_REQUESTED == "candidate.access_requested"
    assert events.CANDIDATE_ACCESS_GRANTED == "candidate.access_granted"
    assert events.CANDIDATE_ACCESS_DECLINED == "candidate.access_declined"


def test_every_kind_fits_the_column() -> None:
    """`event_kind` is String(48). A kind that does not fit fails at insert,
    in production, on the first share."""
    for name in dir(events):
        if name.startswith("CANDIDATE_"):
            assert len(getattr(events, name)) <= 48


def test_a_candidate_event_carries_recipients() -> None:
    import uuid

    from app.services.notify.candidate_events import CandidateEvent

    event = CandidateEvent(
        kind=events.CANDIDATE_SHARED,
        tenant_id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        candidate_name="Wei Ming Tan",
        recipient_user_ids=(uuid.uuid4(),),
    )
    # `None` keeps the tenant-wide meaning, exactly as OpportunityEvent's does.
    assert event.recipient_user_ids is not None
    assert CandidateEvent(
        kind=events.CANDIDATE_UNCLAIMED,
        tenant_id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        candidate_name="Wei Ming Tan",
    ).recipient_user_ids is None
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_notifications.py -v`
Expected: FAIL — `AttributeError: CANDIDATE_SHARED`.

- [ ] **Step 3: Add the kinds**

Append to `backend/app/services/notify/events.py`:

```python
# Candidates. Absorbed as constants with no migration — `event_kind` is a
# String(48) rather than an enum precisely so a new kind costs nothing.
CANDIDATE_SHARED = "candidate.shared"
CANDIDATE_ASSIGNED = "candidate.assigned"
# Releasing to the queue tells the agency: a released candidate is queue work
# again, and nobody would otherwise learn it is available.
CANDIDATE_UNCLAIMED = "candidate.unclaimed"
CANDIDATE_ACCESS_REQUESTED = "candidate.access_requested"
CANDIDATE_ACCESS_GRANTED = "candidate.access_granted"
# Not optional politeness. A request that silently never resolves leaves the
# requester believing it is pending, and they ask again.
CANDIDATE_ACCESS_DECLINED = "candidate.access_declined"
```

- [ ] **Step 4: Add the event type and its emit path**

Create `backend/app/services/notify/candidate_events.py`:

```python
"""What the agency is told about a candidate.

A separate dataclass rather than a widened `OpportunityEvent`: that type's
`job_title`, `company_name`, `location` and `salary` feed the WhatsApp
templates, and a candidate has none of them. Widening it would put four
permanently-None fields in front of every template author.

What the dispatch machinery actually keys on is narrow — `kind`, `tenant_id`,
`recipient_user_ids`, and one subject id — so the two types meet at that
protocol and nowhere else.
"""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateEvent:
    kind: str
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_name: str | None
    # Who should hear about this. `None` keeps the tenant-wide meaning, so a
    # broadcast is one event with N recipients rather than N events — which is
    # what keeps the per-subscriber hourly cap behaving.
    recipient_user_ids: tuple[uuid.UUID, ...] | None = None
    actor_name: str | None = None
    note: str | None = None

    @property
    def subject_id(self) -> uuid.UUID:
        return self.candidate_id
```

In `dispatch.py`, `_write_rows` (155-177) already does `recipients = list(event.recipient_user_ids) if event.recipient_user_ids is not None else None`, and line 177 writes `"subject_id": event.opportunity_id`. Give `OpportunityEvent` the same `subject_id` property, change that line to `event.subject_id`, and add `emit_candidate_event` alongside `emit_and_enqueue` sharing the body.

**Emit is two lines. Delivery is not, and stopping here would ship a queue of rows that crash the worker.** `deliver_notification` rebuilds the event from the outbox row: `jobs.py:1240` fetches the subject with `_DELIVERY_SUBJECT` — a query against **opportunities** — and `jobs.py:1282` constructs an `OpportunityEvent` from it. A `candidate.*` row therefore either finds no subject or reaches `render.py`, where `_HEADLINE[event.kind]` and `_TEMPLATE_FOR[event.kind]` (render.py:36-46) hold only the four opportunity kinds and raise `KeyError`.

- [ ] **Step 5: Make the delivery path handle a candidate**

Three changes, all in the worker's read path:

1. Add the six kinds to `ALL_EVENT_KINDS` (`events.py:16`). Without this nobody can be subscribed to them, so `_write_rows` finds no subscribers and every event silently delivers to no one — which looks exactly like the feature working quietly.
2. In `deliver_notification`, branch on the kind prefix before the subject fetch: a `candidate.*` row queries `candidates` for `full_name` and builds a `CandidateEvent`; everything else keeps the existing path unchanged.
3. Add `_HEADLINE` and `_TEMPLATE_FOR` entries for all six kinds in `render.py`, and a candidate render branch. Copy the shortest existing template's shape — these messages say who did what to which person, and carry no salary or company.

If the customer would rather ship the in-app records first and the outbound messages later, that is a legitimate choice — but it must be **written down as a decision**, and the worker must then skip `candidate.*` rows explicitly rather than crashing on them. Silence here is the failure mode: a crashing delivery worker retries, and a retry loop on a poisoned row stalls every other notification behind it.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_candidate_notifications.py tests/ -k notif -v`
Expected: PASS, including the existing opportunity notification tests — `subject_id` must not have changed their behaviour.

Add one test that drives a `candidate.shared` row all the way through `deliver_notification`, not just through emit. The emit-only test above would pass on a system whose worker crashes.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/notify backend/tests/test_candidate_notifications.py
git commit -m "feat: add candidate notification events"
```

---

## Task 7: guard every candidate route

The predicate is useless until every route calls it, and the failure mode being guarded against is a route added next month. `test_opportunity_routes_guarded.py` exists because two leaks — in `sourcing.py` and `candidates.py` — survived every per-task review.

**Files:**
- Modify: `backend/app/api/candidates.py` (all 12 routes)
- Create: `backend/tests/test_candidate_routes_guarded.py`

- [ ] **Step 1: Write the failing structural test**

Create `backend/tests/test_candidate_routes_guarded.py` by **copying `test_opportunity_routes_guarded.py` verbatim** and changing only what follows. Copy rather than retype: the transitive `_reachable` walk and the `_queries_the_model` verb detection are the parts that caught the real leaks.

```python
READ_GUARD = "load_visible_candidate"
EDIT_GUARD = "load_editable_candidate"
EDIT_CHECK = "can_edit_candidate"
PREDICATE = "visible_candidates"

IN_SCOPE_NAMES = {"Candidate", READ_GUARD, EDIT_GUARD, PREDICATE}

# `_takes_a_job_order_param` becomes `_takes_a_candidate_param` with:
CANDIDATE_PARAM_HINTS = ("candidate",)

EXEMPT: dict[str, dict[str, str]] = {
    "candidates.py": {
        "list_candidates": (
            "Lists rather than loading one by id; it applies "
            "`visible_candidates` directly, which "
            "`test_list_filters_by_the_predicate` asserts."
        ),
        "create_candidate": (
            "Builds a candidate rather than reaching for somebody else's. Its "
            "collision check queries the WHOLE tenant on purpose — the unique "
            "index spans the tenant, so a visibility-filtered check would let "
            "the insert fail on a constraint instead of returning the 409."
        ),
    },
    "candidate_ownership.py": {
        "claim_candidate": (
            "Claiming an UNOWNED candidate is exactly the case "
            "`can_edit_candidate` refuses, so it cannot go through the edit "
            "guard — claiming is the act that creates edit rights. It still "
            "passes the READ assertion, which it is not exempt from."
        ),
    },
    "candidate_shares.py": {
        "request_candidate_access": (
            "The one route deliberately reachable for an INVISIBLE candidate. "
            "That is the entire point of the access-request path, and it "
            "returns nothing whatsoever about the row it names."
        ),
    },
}
```

Note the shape of the two exemptions above: `claim_candidate` is exempt from the EDIT assertion only and still fails the READ one, the same split `sourcing.py::start_sourcing` uses in the reference file. Keep the reference's per-module exemption structure — a flat set of names is a smaller version of the bug the file exists to prevent.

Rewrite the two tests that name modules:

```python
def test_the_guard_covers_more_than_one_module() -> None:
    modules = {m.name for m in _modules()}
    assert {"candidates.py", "candidate_shares.py", "candidate_ownership.py"} <= modules, modules
    assert len({m.name for m, _, _ in _in_scope_routes()}) > 1


def test_list_filters_by_the_predicate() -> None:
    source = (API_DIR / "candidates.py").read_text()
    start = source.index("async def list_candidates")
    end = source.index("\n@router.", start)
    assert f"{PREDICATE}(" in source[start:end], (
        "list_candidates does not apply the visibility predicate"
    )
```

Mark `test_the_guard_covers_more_than_one_module` and `test_every_exemption_names_a_route_that_exists` with `@pytest.mark.xfail(reason="candidate_shares.py and candidate_ownership.py land in Tasks 8 and 11", strict=True)`. **`strict=True` matters**: it makes the mark itself fail once the modules exist, so Task 11 cannot forget to remove it.

- [ ] **Step 2: Run it to see the real list of unguarded routes**

Run: `uv run pytest tests/test_candidate_routes_guarded.py -v`
Expected: FAIL, listing every by-id route in `candidates.py`; the two marked tests `xfail`.

- [ ] **Step 3: Add the guards**

In `backend/app/api/candidates.py`, inside each route's existing `async with tenant_session(tenant_uuid) as session:` block, replace the by-id load.

Read routes — `get_candidate` (492), `export_candidate` (1248), `log_candidate_activity` (1281), `list_candidate_activities` (1325):

```python
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_visible_candidate(session, candidate_uuid, user_uuid, role)
```

Mutating routes — `update_candidate` (925), `archive_candidate` (1013), `restore_candidate` (1029), `unmerge_candidate` (1179), and `merge_candidates` (1052) for now:

```python
        candidate = await load_editable_candidate(session, candidate_uuid, user_uuid, role)
```

`merge_candidates` needs **both** sides; Task 12 does that. Guarding its own id here keeps it from being unguarded between tasks.

`list_candidates` (233) gets the predicate in its `WHERE`:

```python
        stmt = stmt.where(visible_candidates(user_uuid, role))
```

`delete_candidate` (1230) already goes through `_require_owner` and is left alone — it is stricter than anything here.

`log_candidate_activity` takes the **read** guard deliberately: the row it writes is a `candidate_activities` row, not the candidate. A share recipient may record that they opened a WhatsApp chat — that is a fact about what they did.

Routes that used `_require_session(request)` and now need the role must switch to `_require_session_with_role(request)`, which is async and returns `(user_uuid, tenant_uuid, role)`.

Add the imports:

```python
from app.services.visibility import (
    can_edit_candidate,
    load_editable_candidate,
    load_visible_candidate,
    visible_candidates,
)
```

- [ ] **Step 4: Run the structural and behavioural tests**

Run: `uv run pytest tests/test_candidate_routes_guarded.py -v`
Expected: the read and edit assertions PASS; two tests `xfail`.

Run: `uv run pytest -v`
Expected: some existing candidate API tests now 404 — they were written when everything was visible to everyone. Fix them by giving their fixture candidates an `owner_id` matching the signed-in user. **Do not fix them by weakening the predicate.**

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/candidates.py backend/tests
git commit -m "feat: guard every candidate route with the visibility predicate"
```

---

## Task 8: the colliding create

**Files:**
- Modify: `backend/app/api/candidates.py` (`create_candidate`, 847-905)
- Test: `backend/tests/test_candidate_collision.py`

**Interfaces:**
- Produces: `_held_by_colleague(session, match, user_id, role) -> dict | None`, reused by Task 9.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_candidate_collision.py`:

```python
"""Two recruiters, one person — the moment they find out.

Per-tenant email/phone uniqueness is unchanged, so the second recruiter to
type an email cannot create a row. What they get instead is the whole design
decision: enough to act on, and nothing more.
"""

import pytest

from tests.conftest import make_candidate, make_user, sign_in


@pytest.mark.asyncio
async def test_creating_a_candidate_a_colleague_holds_returns_a_thin_409(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, colleague, _ = await make_tenant("agency-collision")
    me = await make_user(admin_session, tenant_id, "me@agency.test")
    await make_candidate(
        admin_session,
        tenant_id,
        owner_id=colleague,
        full_name="Wei Ming Tan",
        email="weiming@example.com",
        phone_e164="+6591234567",
        current_title="Senior Backend Engineer",
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        "/api/candidates",
        json={"full_name": "Wei Ming Tan", "email": "weiming@example.com"},
    )

    assert response.status_code == 409
    body = response.json()["detail"]
    assert body["reason"] == "already_registered"
    assert body["can_request_access"] is True
    # The disclosure is deliberate and bounded: who holds them, and a name
    # short enough to recognise. Nothing else crosses the boundary.
    assert set(body["candidate"]) == {"full_name", "held_by"}
    assert "+6591234567" not in response.text
    assert "Senior Backend Engineer" not in response.text


@pytest.mark.asyncio
async def test_a_conflicting_match_is_not_a_collision(client, admin_session, seeded) -> None:
    """Email and phone pointing at two different people. The system does not
    know which person is meant, so it cannot name one — and offers no
    request-access."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-conflict")
    await make_candidate(
        admin_session, tenant_id, owner_id=me, email="a@example.com", phone_e164="+6590000001"
    )
    await make_candidate(
        admin_session, tenant_id, owner_id=me, email="b@example.com", phone_e164="+6590000002"
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        "/api/candidates",
        json={"full_name": "Someone", "email": "a@example.com", "phone_raw": "+6590000002"},
    )
    assert response.status_code == 409
    assert "can_request_access" not in response.text
```

The 409 body is nested under FastAPI's `detail`, which is why the test reads `response.json()["detail"]["reason"]` — `detail` is FastAPI's own key and cannot also be ours.

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_collision.py -v`
Expected: FAIL — the existing generic 409 body has no `reason`.

- [ ] **Step 3: Add the helper and wire it into create**

In `backend/app/api/candidates.py`, above `create_candidate`:

```python
def _abbreviate(full_name: str) -> str:
    """"Wei Ming Tan" -> "Wei Ming T." — enough to recognise a person you have
    met, not enough to be a directory of who the agency holds."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    return " ".join([*parts[:-1], f"{parts[-1][0]}."])


async def _held_by_colleague(
    session: AsyncSession,
    match: MatchResult,
    user_id: uuid.UUID,
    role: str,
) -> dict[str, object] | None:
    """The 409 body for a candidate that exists but is not ours to see.

    Returns None when the match is visible to the caller — that is the
    ordinary duplicate the route already handled, and the caller can simply be
    sent to the row.

    The disclosure here is deliberate. The caller learns this person is in the
    agency's database and who holds them, and nothing else: no contact detail,
    no salary, no notes, no client history, and not even the candidate id. The
    alternative is a wall a recruiter cannot act on, and in a three-to-fifty
    person agency they will walk to that colleague's desk and ask anyway.
    """
    if match.candidate_id is None:
        return None

    visible = (
        await session.execute(
            select(Candidate.id)
            .where(Candidate.id == match.candidate_id)
            .where(visible_candidates(user_id, role))
        )
    ).scalar_one_or_none()
    if visible is not None:
        return None

    row = (
        await session.execute(
            select(
                Candidate.full_name,
                # `users` has no `full_name`. Prefer what the person chose to
                # be called, then what the directory says, then the address
                # they signed in with — never a bare UUID, which tells the
                # recruiter nothing and looks like a bug.
                func.coalesce(User.preferred_name, User.display_name, User.email).label("holder"),
            )
            .join(User, User.id == Candidate.owner_id, isouter=True)
            .where(Candidate.id == match.candidate_id)
        )
    ).one()

    return {
        "reason": "already_registered",
        "candidate": {"full_name": _abbreviate(row.full_name), "held_by": row.holder},
        "can_request_access": True,
    }
```

In `create_candidate`, after the existing `find_candidate` call and **before** the existing `match.candidate_id` duplicate branch:

```python
        held = await _held_by_colleague(session, match, user_uuid, role)
        if held is not None:
            raise HTTPException(status_code=409, detail=held)
```

The `conflict` branch above it is untouched: the system does not know which person is meant, so it cannot name one.

Set the owner on insert — you typed it in, it is yours:

```python
        values["owner_id"] = user_uuid
```

`create_candidate` needs the role, so switch its session line to `_require_session_with_role(request)`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_candidate_collision.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/candidates.py backend/tests/test_candidate_collision.py
git commit -m "feat: answer a colliding candidate create with a thin 409"
```

---

## Task 9: the colliding PATCH

The easier one to forget, and it fails worse: changing an email to a value an invisible row holds hits `uq_candidates_tenant_email` at flush time and surfaces as a 500 with a constraint name in it.

**Files:**
- Modify: `backend/app/api/candidates.py` (`update_candidate`, 925)
- Test: `backend/tests/test_candidate_collision.py` (append)

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_patching_an_email_onto_a_colleagues_candidate_returns_409_not_500(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, colleague, _ = await make_tenant("agency-patch-collision")
    me = await make_user(admin_session, tenant_id, "me2@agency.test")
    await make_candidate(
        admin_session,
        tenant_id,
        owner_id=colleague,
        full_name="Wei Ming Tan",
        email="weiming@example.com",
    )
    mine = await make_candidate(
        admin_session, tenant_id, owner_id=me, full_name="Wei M Tan", email="typo@example.com"
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.patch(
        f"/api/candidates/{mine}", json={"email": "weiming@example.com"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "already_registered"
    assert "uq_candidates_tenant_email" not in response.text


@pytest.mark.asyncio
async def test_patching_without_touching_the_email_is_not_a_collision(
    client, admin_session, seeded
) -> None:
    """The row being edited matches itself. That is not a collision, and
    treating it as one would make every edit impossible."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-patch-self")
    mine = await make_candidate(
        admin_session, tenant_id, owner_id=me, email="self@example.com"
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.patch(f"/api/candidates/{mine}", json={"current_title": "CTO"})
    assert response.status_code == 200
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_collision.py -v`
Expected: the first new test FAILs with 500 and `uq_candidates_tenant_email` in the body.

- [ ] **Step 3: Add the check to PATCH**

In `update_candidate`, after the candidate is loaded through `load_editable_candidate` and the new values are parsed, before the `update(Candidate)` executes:

```python
        # A typo correction is as likely to collide as a fresh create, and the
        # recruiter fixing it has as much reason to learn the person is held.
        if "email" in values or "phone_e164" in values:
            match = await find_candidate(
                session,
                tenant_uuid,
                values.get("email", candidate.email),
                values.get("phone_e164", candidate.phone_e164),
            )
            # A PATCH that leaves the keys alone matches the row being edited.
            # That is not a collision.
            if match.candidate_id is not None and match.candidate_id != candidate.id:
                held = await _held_by_colleague(session, match, user_uuid, role)
                if held is not None:
                    raise HTTPException(status_code=409, detail=held)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_candidate_collision.py -v`
Expected: all four PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/candidates.py backend/tests/test_candidate_collision.py
git commit -m "fix: return the thin 409 from PATCH instead of a constraint 500"
```

---

## Task 10: claim, assign, and the scope filter

**Files:**
- Create: `backend/app/api/candidate_ownership.py`
- Modify: `backend/app/main.py`, `backend/app/api/candidates.py` (`list_candidates`)
- Test: `backend/tests/test_candidate_ownership_api.py`

**Interfaces:**
- Produces: `POST /api/candidates/{id}/claim`, `POST /api/candidates/{id}/assign`, `GET /api/candidates?scope=mine|queue|shared_with_me|all`.

- [ ] **Step 1: Write the failing test**

```python
"""Claiming from the queue, and handing a candidate over."""

import pytest

from tests.conftest import make_candidate, make_user, sign_in


@pytest.mark.asyncio
async def test_two_claims_produce_one_winner(client, admin_session, seeded) -> None:
    """The race two recruiters will genuinely hit in a 9pm rush."""
    make_tenant, _, _ = seeded
    tenant_id, first, _ = await make_tenant("agency-claim-race")
    second = await make_user(admin_session, tenant_id, "second@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=None)
    await admin_session.commit()

    sign_in(client, first, tenant_id)
    one = await client.post(f"/api/candidates/{candidate_id}/claim")
    sign_in(client, second, tenant_id)
    two = await client.post(f"/api/candidates/{candidate_id}/claim")

    assert sorted([one.status_code, two.status_code]) == [200, 409]


@pytest.mark.asyncio
async def test_scope_filters_cannot_widen_visibility(client, admin_session, seeded) -> None:
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-scope")
    colleague = await make_user(admin_session, tenant_id, "other@agency.test")
    for owner in (me, colleague, None):
        await make_candidate(admin_session, tenant_id, owner_id=owner)
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    everything = {
        row["id"] for row in (await client.get("/api/candidates?scope=all")).json()["items"]
    }
    for scope in ("mine", "queue", "shared_with_me"):
        subset = {
            row["id"]
            for row in (await client.get(f"/api/candidates?scope={scope}")).json()["items"]
        }
        assert subset <= everything, f"scope={scope} widened visibility"


@pytest.mark.asyncio
async def test_releasing_to_the_queue_tells_the_agency(client, admin_session, seeded) -> None:
    from sqlalchemy import text

    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-release")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=me)
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    assert (
        await client.post(f"/api/candidates/{candidate_id}/assign", json={"user_id": None})
    ).status_code == 200

    kinds = (
        await admin_session.execute(
            text("SELECT event_kind FROM notification_outbox WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    ).scalars().all()
    assert "candidate.unclaimed" in kinds
```

Check the real outbox table and column names before running that last assertion — read `dispatch.py`'s insert rather than trusting the names here.

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_ownership_api.py -v`
Expected: FAIL — 404 on `/claim`.

- [ ] **Step 3: Write the module**

Create `backend/app/api/candidate_ownership.py`:

```python
"""Claiming a candidate out of the queue, and handing one over.

Separate from `candidates.py` only because that file is at 1458 of the 1500
LOC limit. If it is ever split properly, these belong beside it.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import update

from app.api.auth import _require_session_with_role
from app.db.rls import tenant_session
from app.models.candidate import Candidate
from app.services.notify.candidate_events import CandidateEvent, emit_candidate_event
from app.services.notify.events import CANDIDATE_ASSIGNED, CANDIDATE_UNCLAIMED
from app.services.visibility import can_edit_candidate, load_visible_candidate

router = APIRouter(tags=["candidate-ownership"])


class AssignBody(BaseModel):
    user_id: uuid.UUID | None


@router.post("/candidates/{candidate_id}/claim")
async def claim_candidate(candidate_id: uuid.UUID, request: Request) -> dict[str, str]:
    """An atomic UPDATE, not a read-then-write.

    Two recruiters claiming the same candidate at the same moment is a real
    race, not a theoretical one. `WHERE owner_id IS NULL` resolves it in the
    database; the loser gets 409 rather than silently overwriting the winner.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        await load_visible_candidate(session, candidate_id, user_uuid, role)
        result = await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .where(Candidate.owner_id.is_(None))
            .values(owner_id=user_uuid, updated_by=user_uuid)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="A colleague claimed this first.")

    # Nothing is emitted: you did it, you know.
    return {"status": "claimed"}


@router.post("/candidates/{candidate_id}/assign")
async def assign_candidate(
    candidate_id: uuid.UUID, body: AssignBody, request: Request
) -> dict[str, str]:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        candidate = await load_visible_candidate(session, candidate_id, user_uuid, role)
        if not can_edit_candidate(candidate, user_uuid, role):
            raise HTTPException(status_code=403, detail="This candidate is not yours to assign.")
        name = candidate.full_name
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(owner_id=body.user_id, updated_by=user_uuid)
        )

    # Emitted AFTER the session closes, as `opportunity_shares.py` does: a
    # rolled-back transaction must not leave a notification claiming something
    # that did not happen.
    #
    # A released candidate is queue work again and nobody would otherwise
    # learn it is available, so releasing tells the agency; handing over tells
    # one person.
    await emit_candidate_event(
        CandidateEvent(
            kind=CANDIDATE_UNCLAIMED if body.user_id is None else CANDIDATE_ASSIGNED,
            tenant_id=tenant_uuid,
            candidate_id=candidate_id,
            candidate_name=name,
            recipient_user_ids=None if body.user_id is None else (body.user_id,),
        )
    )
    return {"status": "assigned"}
```

Add the `scope` query parameter to `list_candidates`, filtering **within** the predicate, never replacing it:

```python
        # These narrow what the predicate already allows. Composing with
        # `.where` rather than replacing it is what makes "cannot widen" true
        # by construction rather than by review.
        if scope == "mine":
            stmt = stmt.where(Candidate.owner_id == user_uuid)
        elif scope == "queue":
            stmt = stmt.where(Candidate.owner_id.is_(None))
        elif scope == "shared_with_me":
            stmt = stmt.where(candidate_shared_with_me_exists(user_uuid))
```

Register the router in `backend/app/main.py` beside the candidates router, under `/api` — `tests/test_routing.py` fails if a route escapes `/api`, where the static mount would shadow it.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_candidate_ownership_api.py tests/test_routing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/candidate_ownership.py backend/app/api/candidates.py backend/app/main.py backend/tests/test_candidate_ownership_api.py
git commit -m "feat: claim, assign and scope-filter candidates"
```

---

## Task 11: the share and access-request API

**Files:**
- Create: `backend/app/api/candidate_shares.py`
- Modify: `backend/app/main.py`, `backend/tests/test_candidate_routes_guarded.py`
- Test: `backend/tests/test_candidate_shares_api.py` (append)
- Mirror: `backend/app/api/opportunity_shares.py` (256 LOC)

**Interfaces:**
- Produces: `POST/GET/DELETE /api/candidates/{id}/shares`; `POST /api/candidates/{id}/access-requests`; `POST /api/candidates/{id}/access-requests/{req_id}/grant`; `.../decline`; `GET /api/candidates/access-requests?status=pending` — the inbox, which has no equivalent to mirror and must be written fresh.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_candidate_shares_api.py`:

```python
@pytest.mark.asyncio
async def test_a_recipient_may_share_onward_but_not_broadcast(
    client, admin_session, seeded
) -> None:
    """A candidate finds the right person through a chain of individual
    shares. Throwing a colleague's candidate open to the whole office is not
    the recipient's decision to make."""
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-onward")
    recipient = await make_user(admin_session, tenant_id, "r@agency.test")
    third = await make_user(admin_session, tenant_id, "t@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    admin_session.add(
        CandidateShare(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            scope=CandidateShare.SCOPE_USER,
            shared_with_user_id=recipient,
            shared_by_user_id=owner,
        )
    )
    await admin_session.commit()

    sign_in(client, recipient, tenant_id)
    onward = await client.post(
        f"/api/candidates/{candidate_id}/shares",
        json={"scope": "user", "user_ids": [str(third)]},
    )
    assert onward.status_code == 201

    broadcast = await client.post(
        f"/api/candidates/{candidate_id}/shares", json={"scope": "tenant"}
    )
    assert broadcast.status_code == 403


@pytest.mark.asyncio
async def test_resharing_updates_the_note_rather_than_409ing(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-reshare")
    colleague = await make_user(admin_session, tenant_id, "c@agency.test")
    candidate_id = await make_candidate(admin_session, tenant_id, owner_id=owner)
    await admin_session.commit()

    sign_in(client, owner, tenant_id)
    body = {"scope": "user", "user_ids": [str(colleague)], "note": "first"}
    assert (await client.post(f"/api/candidates/{candidate_id}/shares", json=body)).status_code == 201
    body["note"] = "second"
    assert (await client.post(f"/api/candidates/{candidate_id}/shares", json=body)).status_code == 201

    row = (
        await admin_session.execute(
            text("SELECT note, count(*) OVER () AS n FROM candidate_shares WHERE candidate_id = :c"),
            {"c": candidate_id},
        )
    ).one()
    assert row.note == "second"
    assert row.n == 1, "re-sharing duplicated instead of updating — check index_where"


@pytest.mark.asyncio
async def test_requesting_access_leaks_nothing_and_granting_creates_one_share(
    client, admin_session, seeded
) -> None:
    make_tenant, _, _ = seeded
    tenant_id, owner, _ = await make_tenant("agency-request-flow")
    asker = await make_user(admin_session, tenant_id, "asker2@agency.test")
    candidate_id = await make_candidate(
        admin_session, tenant_id, owner_id=owner, full_name="Secret Person"
    )
    await admin_session.commit()

    sign_in(client, asker, tenant_id)
    # Invisible right now.
    assert (await client.get(f"/api/candidates/{candidate_id}")).status_code == 404

    asked = await client.post(
        f"/api/candidates/{candidate_id}/access-requests", json={"note": "same person I met"}
    )
    assert asked.status_code == 200
    assert "Secret Person" not in asked.text, "the request route revealed the row"
    request_id = asked.json()["id"]

    # A second click must not spam the owner.
    assert (
        await client.post(f"/api/candidates/{candidate_id}/access-requests", json={})
    ).status_code == 200

    sign_in(client, owner, tenant_id)
    inbox = await client.get("/api/candidates/access-requests?status=pending")
    assert inbox.status_code == 200
    assert len(inbox.json()["items"]) == 1

    granted = await client.post(
        f"/api/candidates/{candidate_id}/access-requests/{request_id}/grant"
    )
    assert granted.status_code == 200

    shares = (
        await admin_session.execute(
            text("SELECT count(*) AS n FROM candidate_shares WHERE candidate_id = :c"),
            {"c": candidate_id},
        )
    ).one()
    assert shares.n == 1

    sign_in(client, asker, tenant_id)
    assert (await client.get(f"/api/candidates/{candidate_id}")).status_code == 200
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/test_candidate_shares_api.py -v`
Expected: FAIL — 404 on every new route.

- [ ] **Step 3: Write the module**

Create `backend/app/api/candidate_shares.py` by mirroring `backend/app/api/opportunity_shares.py`: the same `_require_session_with_role` + `async with tenant_session(...)` shape, the same `pg_insert(...).on_conflict_do_update(index_elements=[...], index_where=_WHERE_USER_SCOPE)` upsert with module-level literals

```python
_WHERE_USER_SCOPE = text("scope = 'user'")
_WHERE_TENANT_SCOPE = text("scope = 'tenant'")
```

and the same emit-after-commit ordering. Four things differ:

```python
@router.post("/candidates/{candidate_id}/shares", status_code=201)
async def create_candidate_share(
    candidate_id: uuid.UUID, body: ShareRequest, request: Request
) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        candidate = await load_visible_candidate(session, candidate_id, user_uuid, role)

        # A recipient may pass a candidate onward to a named colleague — that
        # is how a person finds the right recruiter through a chain. Throwing
        # somebody else's candidate open to the whole office is not theirs to
        # decide, so the broadcast is restricted to the owner and role='owner'.
        if body.scope == CandidateShare.SCOPE_TENANT and not can_edit_candidate(
            candidate, user_uuid, role
        ):
            raise HTTPException(
                status_code=403,
                detail="Only the owner may share this candidate with everyone.",
            )
        ...


@router.post("/candidates/{candidate_id}/access-requests")
async def request_candidate_access(
    candidate_id: uuid.UUID, body: AccessRequestBody, request: Request
) -> dict[str, str]:
    """The one route deliberately reachable for a candidate you cannot see.

    It therefore does NOT call `load_visible_candidate` — that is the whole
    point of it, and `test_candidate_routes_guarded.py` exempts it by name
    with this reason. It still checks the row exists in this tenant, because a
    request against a random UUID should 404 rather than sit pending forever,
    and it returns nothing whatsoever about the row.
    """
    user_uuid, tenant_uuid, _role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        exists = (
            await session.execute(select(Candidate.id).where(Candidate.id == candidate_id))
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=404, detail="No such candidate.")

        stmt = (
            pg_insert(CandidateAccessRequest)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                candidate_id=candidate_id,
                requested_by_user_id=user_uuid,
                status=CandidateAccessRequest.STATUS_PENDING,
                note=body.note,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "candidate_id", "requested_by_user_id"],
                index_where=_WHERE_PENDING,
                set_={"note": body.note},
            )
            .returning(CandidateAccessRequest.id)
        )
        request_id = (await session.execute(stmt)).scalar_one()
        owner_id = (
            await session.execute(
                select(Candidate.owner_id).where(Candidate.id == candidate_id)
            )
        ).scalar_one()

    await emit_candidate_event(
        CandidateEvent(
            kind=CANDIDATE_ACCESS_REQUESTED,
            tenant_id=tenant_uuid,
            candidate_id=candidate_id,
            candidate_name=None,
            recipient_user_ids=(owner_id,) if owner_id else None,
        )
    )
    return {"id": str(request_id)}


@router.get("/candidates/access-requests")
async def list_my_access_requests(request: Request, status: str = "pending") -> dict:
    """Requests waiting on ME — the inbox. There is no route to mirror for
    this; the opportunity side never grew one because it has no request flow.

    Scoped to candidates the caller owns, so it is not a listing of every
    request in the agency. `role='owner'` sees all of them, consistently with
    every other predicate here.
    """
```

`grant` creates the share and marks the request `granted` with `resolved_at` and `resolved_by_user_id`, **in one transaction** — the share is the grant, and a granted request with no share would be a lie. `decline` sets `declined` and the same resolution columns, creates nothing, and emits `CANDIDATE_ACCESS_DECLINED` to the requester. Both are permitted to the candidate's owner and to `role='owner'`.

`DELETE /candidates/{id}/shares/{share_id}` is permitted to the owner, the original sharer, `role='owner'`, and a recipient removing themselves — wider than `can_edit_candidate`, which is why it applies the check itself rather than loading through the edit guard, exactly as `opportunity_shares.py` does. Unsharing revokes sight, not history: the recipient's logged activities stay.

`GET /candidates/{id}/shares` mirrors `opportunity_shares.py:181` directly.

Register the router in `backend/app/main.py` by inserting `api.include_router(candidate_shares.router)` **above line 151**, where `candidates.router` is included. FastAPI matches routes in `include_router` order, so registering it after would let `GET /candidates/{candidate_id}` capture `access-requests` as a UUID path parameter and 422 — a failure that looks like a broken inbox rather than a routing order.

- [ ] **Step 4: Remove the xfail marks and run everything**

Delete the two `@pytest.mark.xfail` marks from `test_candidate_routes_guarded.py`. With `strict=True` they fail loudly now that the modules exist, so this step cannot be silently skipped.

Run: `uv run pytest tests/test_candidate_shares_api.py tests/test_candidate_routes_guarded.py tests/test_routing.py -v`
Expected: PASS, all assertions live.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/candidate_shares.py backend/app/main.py backend/tests/test_candidate_shares_api.py backend/tests/test_candidate_routes_guarded.py
git commit -m "feat: share candidates and request access to a colleague's"
```

---

## Task 12: merge and unmerge

**Files:**
- Modify: `backend/app/api/candidates.py` (`merge_candidates` 1052, `unmerge_candidate` 1179)
- Test: `backend/tests/test_candidate_merge_ownership.py`

- [ ] **Step 1: Write the failing test**

```python
"""Merging destroys one of two records. Both must be yours to destroy."""

import pytest
from sqlalchemy import text

from tests.conftest import make_candidate, make_user, sign_in


@pytest.mark.asyncio
async def test_merge_needs_edit_rights_on_both_sides(client, admin_session, seeded) -> None:
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-merge-rights")
    colleague = await make_user(admin_session, tenant_id, "mc@agency.test")
    mine = await make_candidate(admin_session, tenant_id, owner_id=me)
    theirs = await make_candidate(admin_session, tenant_id, owner_id=colleague)
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    response = await client.post(
        f"/api/candidates/{mine}/merge", json={"into_candidate_id": str(theirs)}
    )
    assert response.status_code in (403, 404)

    still_there = (
        await admin_session.execute(
            text("SELECT record_status FROM candidates WHERE id = :id"), {"id": mine}
        )
    ).one()
    assert still_there.record_status == "active"


@pytest.mark.asyncio
async def test_unmerge_restores_the_original_owner(client, admin_session, seeded) -> None:
    """The revived row goes back to whoever held it, not to whoever pressed
    the button."""
    make_tenant, _, _ = seeded
    tenant_id, me, _ = await make_tenant("agency-unmerge-owner")
    target = await make_candidate(admin_session, tenant_id, owner_id=me)
    merged = await make_candidate(
        admin_session,
        tenant_id,
        owner_id=me,
        record_status="merged",
        merged_into_candidate_id=target,
    )
    await admin_session.commit()

    sign_in(client, me, tenant_id)
    assert (await client.post(f"/api/candidates/{merged}/unmerge")).status_code == 200

    row = (
        await admin_session.execute(
            text("SELECT owner_id, record_status FROM candidates WHERE id = :id"), {"id": merged}
        )
    ).one()
    assert row.record_status == "active"
    assert row.owner_id == me, "unmerge reassigned the row to whoever pressed the button"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_merge_ownership.py -v`
Expected: FAIL — the merge succeeds against a candidate the caller does not own.

- [ ] **Step 3: Check both sides**

In `merge_candidates`, load both through the edit guard:

```python
        # Merging is destructive on one side and additive on the other, so the
        # caller must hold both. The realistic case — B discovers, after being
        # granted access, that they and A hold the same person — is not a
        # merge B performs. Until a cross-owner merge request exists,
        # `role='owner'` is the escape hatch, which is workable in an agency
        # where the boss is one desk away.
        source = await load_editable_candidate(session, candidate_uuid, user_uuid, role)
        target = await load_editable_candidate(session, into_uuid, user_uuid, role)
```

`unmerge_candidate` needs no ownership change at all. Add the comment, because the temptation to "fix" it is exactly the bug the test guards:

```python
        # `owner_id` is deliberately not written here. It survived the merge on
        # this row, so reviving the row restores its original owner. If that
        # recruiter has since been deleted the column is already NULL and the
        # row lands in the queue — the same outcome every other path gives.
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_candidate_merge_ownership.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/candidates.py backend/tests/test_candidate_merge_ownership.py
git commit -m "feat: require edit rights on both sides of a candidate merge"
```

---

## Task 13: imports respect ownership

`apply.py` never touches `uploaded_by` — only `import_id` is threaded, and the tenant is resolved through `_import_tenant` at line 159. The owner has to be fetched and threaded before any of this works.

**Files:**
- Modify: `backend/app/services/imports/apply.py`
- Test: `backend/tests/test_candidate_import_ownership.py`

**Interfaces:**
- Consumes: `CandidateImport.uploaded_by` (`candidate.py:742`), Task 0's `run_import`.
- Produces: `ImportOutcome.held_by_colleagues: int = 0`.

- [ ] **Step 1: Write the failing test**

```python
"""An import is a bulk create and a bulk edit, so it meets both boundaries."""

import pytest
from sqlalchemy import text

from tests.conftest import make_candidate, make_user


@pytest.mark.asyncio
async def test_an_import_owns_the_rows_it_creates(admin_session, seeded, run_import) -> None:
    """Somebody uploading their own contact list does not intend to donate it
    to the shared queue."""
    make_tenant, _, _ = seeded
    tenant_id, importer, _ = await make_tenant("agency-import-owner")
    await admin_session.commit()

    outcome = await run_import(
        tenant_id, importer, [{"full_name": "New Person", "email": "n@x.com"}]
    )
    assert outcome.candidates_created == 1

    row = (
        await admin_session.execute(
            text("SELECT owner_id FROM candidates WHERE email = 'n@x.com'")
        )
    ).one()
    assert row.owner_id == importer


@pytest.mark.asyncio
async def test_an_import_skips_a_row_a_colleague_holds(
    admin_session, seeded, run_import
) -> None:
    """Both cases read the same to the importer, and are worded the same:
    invisible, and visible-but-shared. They may edit neither."""
    make_tenant, _, _ = seeded
    tenant_id, importer, _ = await make_tenant("agency-import-held")
    colleague = await make_user(admin_session, tenant_id, "held@agency.test")
    await make_candidate(
        admin_session, tenant_id, owner_id=colleague, full_name="Held Person",
        email="held@x.com",
    )
    await admin_session.commit()

    outcome = await run_import(
        tenant_id,
        importer,
        [{"full_name": "Held Person", "email": "held@x.com", "current_title": "CTO"}],
    )
    assert outcome.held_by_colleagues == 1
    assert outcome.candidates_updated == 0

    unchanged = (
        await admin_session.execute(
            text("SELECT current_title FROM candidates WHERE email = 'held@x.com'")
        )
    ).one()
    assert unchanged.current_title is None, "an import edited a colleague's candidate"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/test_candidate_import_ownership.py -v`
Expected: FAIL — `AttributeError: 'ImportOutcome' object has no attribute 'held_by_colleagues'`.

- [ ] **Step 3: Thread `uploaded_by` and add the counter**

Add to the `ImportOutcome` dataclass:

```python
    # Rows this import found but was not allowed to touch. Named for what the
    # importer sees, not for the mechanism: whether the row is invisible or
    # merely shared is a distinction they cannot act on either way.
    held_by_colleagues: int = 0
```

`_import_tenant` (line 159) resolves the tenant from the `candidate_imports` row. Extend its query and return to carry `uploaded_by` too, and thread that value from `apply_import` (663) into `_apply_candidates` (286) as a parameter.

At the `Candidate(...)` construction (357):

```python
        # `CandidateImport.uploaded_by` already records who ran this. Leaving
        # it out would put a recruiter's whole uploaded contact list into the
        # shared queue.
        owner_id=uploaded_by,
```

In the update branch (341), before it writes anything:

```python
    # Import matching runs against the WHOLE tenant, ignoring visibility, and
    # must: the unique index spans the tenant, so a visibility-filtered lookup
    # would miss an invisible row and then fail on the constraint at flush
    # time with an error nobody can act on.
    #
    # Having found it, an import may still not edit it. An import is a bulk
    # edit, and a row the importer does not own is not theirs to change —
    # whether they cannot see it at all, or can see it only through a share.
    if existing.owner_id not in (None, uploaded_by):
        outcome.held_by_colleagues += 1
        continue
```

Surface `held_by_colleagues` wherever the import UI reads the outcome.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_candidate_import_ownership.py tests/ -k import -v`
Expected: PASS, including the existing import and import-undo tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/imports/apply.py backend/tests/test_candidate_import_ownership.py
git commit -m "feat: imports own what they create and skip what colleagues hold"
```

---

## Task 14: the frontend

Without this the feature is enforced but incomprehensible: "all" silently means "the subset I can see", and a colliding create renders as a red validation error on the email field.

**Files:**
- Modify: `frontend/app/dashboard/candidates/candidates.ts`, `page.tsx`, `candidate-form.tsx`, `candidate-panel.tsx`
- Create: `frontend/app/dashboard/candidates/candidate-share.tsx`
- Test: `frontend/app/dashboard/candidates/candidate-share.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from './candidates'
import { CandidateForm } from './candidate-form'

vi.mock('./candidates')

describe('a candidate a colleague already holds', () => {
  beforeEach(() => {
    vi.mocked(api.createCandidate).mockRejectedValue({
      status: 409,
      body: {
        detail: {
          reason: 'already_registered',
          candidate: { full_name: 'Wei Ming T.', held_by: 'Sarah Lim' },
          can_request_access: true,
        },
      },
    })
  })

  it('offers to ask rather than showing a validation error', async () => {
    render(<CandidateForm />)

    await userEvent.type(screen.getByLabelText(/full name/i), 'Wei Ming Tan')
    await userEvent.type(screen.getByLabelText(/email/i), 'weiming@example.com')
    await userEvent.click(screen.getByRole('button', { name: /save/i }))

    expect(await screen.findByText(/Sarah Lim/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /request access/i })).toBeInTheDocument()
    // The email field is not the problem, and marking it invalid tells the
    // recruiter to change a value that is correct.
    expect(screen.getByLabelText(/email/i)).not.toHaveAttribute('aria-invalid', 'true')
  })
})
```

`CandidateForm` imports its API functions from the module rather than taking them as props, so this mocks the module. Check the real prop signature before writing the render call and adapt.

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd frontend && npx vitest run app/dashboard/candidates/candidate-share.test.tsx`
Expected: FAIL — no "Sarah Lim" in the document.

- [ ] **Step 3: Build the five surfaces**

| Surface | Change |
|---|---|
| `candidates.ts` | Wrap the new routes: `shareCandidate`, `listShares`, `deleteShare`, `requestAccess`, `listAccessRequests`, `grantAccess`, `declineAccess`, `claimCandidate`, `assignCandidate`. Every candidate API call already goes through this module and none is constructed inline — keep it that way; an inline path is the shape that broke before. |
| `page.tsx` | A `scope` filter — mine / queue / shared with me / all — wired to `?scope=`, matching the job order list. |
| `candidate-form.tsx` | Catch the 409, branch on `detail.reason === 'already_registered'`, render "already registered by {held_by}" with a **Request access** button calling `requestAccess`. Not a field error. |
| `candidate-panel.tsx` | Owner name, a share control, and read-only rendering when the caller cannot edit — a **disabled** edit affordance, not a hidden one, so a share recipient understands why rather than thinking the page is broken. |
| `candidate-share.tsx` | The share dialog (pick colleagues, or share with everyone — the latter disabled unless you own the row) and the pending access-request inbox with grant and decline, backed by `GET /api/candidates/access-requests`. |

`candidate-whatsapp.tsx` needs no change: it posts to `/activities`, which a share recipient is permitted.

Keep the dependency list as it is — `frontend/`'s only runtime dependencies are `next`, `react` and `qrcode`, and nothing here needs a fourth.

- [ ] **Step 4: Run the frontend tests and the build**

Run: `cd frontend && npx vitest run && npm run build`
Expected: tests PASS, static export builds.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/dashboard/candidates
git commit -m "feat: candidate ownership, sharing and access requests in the UI"
```

---

## Task 15: full-suite verification and the rollout decision

- [ ] **Step 1: Run the whole backend suite and the linter**

Run: `cd backend && uv run pytest -v && uv run ruff check .`
Expected: PASS, zero lint findings. Quote the real output; do not assert success from memory.

- [ ] **Step 2: Verify the migration chain applies from scratch**

Run: `cd backend && uv run alembic downgrade 314cc3da9ced && uv run alembic upgrade head`

`314cc3da9ced` is the head this work hangs off, so this reverts exactly the four migrations added here and no more. Do not use `a1b2c3d4e5f6` — that is `314cc3da9ced`'s **parent**, and downgrading to it would destroy `opportunity_shares`.

Expected: clean down and up. This is what catches a `downgrade()` that was never tested and a missing RLS block.

- [ ] **Step 3: Check the file-size limit was respected**

Run: `cd backend && wc -l app/api/candidates.py app/api/candidate_shares.py app/api/candidate_ownership.py app/models/candidate.py`
Expected: every file under 1500. `candidates.py` started at 1458; if it has crossed, move `_held_by_colleague` and `_abbreviate` into `app/services/candidate_matching.py` rather than raising the limit.

- [ ] **Step 4: Verify the guard has no leftover exemptions**

Run: `cd backend && grep -n "xfail" tests/test_candidate_routes_guarded.py`
Expected: no output. `strict=True` should already have failed the suite if a mark survived, but this is the one check that costs nothing and catches a silently disabled guard.

- [ ] **Step 5: Raise the two decisions this plan cannot make**

**The 409 discloses "we hold this person, and Sarah has them."** Bounded — an abbreviated name and a holder, no contact detail, no id — and deliberate, because the alternative is a wall a recruiter cannot act on. But under PDPA "we hold this person" is itself information about a data subject. Built as specified; shipping it is the customer's call.

**The deploy is user-visible the moment it lands.** A wholly shared database becomes wholly private: every recruiter loses sight of every candidate they did not create. Two options, and the choice is the customer's:

1. A tenant-wide announcement before the deploy.
2. A transitional `scope='tenant'` share on every pre-existing candidate, removed later — one INSERT per existing candidate, reversible by deleting those rows.

Do not pick one silently. Raise both, and record the answer here before deploying.

- [ ] **Step 6: Update the project status**

In `CLAUDE.md`, add candidate ownership and sharing to "Shipped since" with the date and a link to the design doc, in the same shape as the job order entry.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record candidate ownership and sharing as shipped"
```

---

## Self-Review

**Spec coverage.** Data model → 1-3; per-user readings and the fact/judgement split → 4; visibility → 5, 7; notifications → 6; colliding create → 8; colliding PATCH → 9; claim/assign/scope → 10; shares, access requests and the inbox → 11; merge/unmerge → 12; imports → 13; frontend → 14; migration and rollout → 1, 15.

**Corrections made against the first draft**, each verified in source rather than assumed: the migration head is `314cc3da9ced`, not `a1b2c3d4e5f6`; routes use `async with tenant_session(...)` and never take a `session` parameter; `client`/`seeded`/`sign_in` were not in `conftest.py` and `sign_in` is synchronous (Task 0); `verify_rls_enforced()` takes no arguments; `users` has `preferred_name`/`display_name`, not `full_name`; `pipeline_stage` and `record_status` are NOT NULL with Python-side defaults, so tests build rows through the ORM; `ImportOutcome` is a dataclass with attribute counters and `uploaded_by` is not currently threaded into `apply.py`; `emit_and_enqueue` takes an opportunity-shaped dataclass, so candidates need their own event type (Task 6); `overridden_fields` at `candidate_overrides.py:19` is the rendering read site the spec required and had no task.

**Type consistency.** `visible_candidates` / `can_edit_candidate` / `load_visible_candidate` / `load_editable_candidate` / `candidate_shared_with_me_exists` are spelled identically in Tasks 5, 7, 10, 11 and 12 and in the guard test's constants. `CandidateShare.SCOPE_*` and `CandidateAccessRequest.STATUS_*` are referenced by constant everywhere, never as bare strings. The 409 body is `detail.reason`, not `detail`, in both the API and both test files — `detail` is FastAPI's own key.

**Second-pass corrections**, again verified in source: `apply_import` takes `(session, *, tenant_id, import_id, candidates, roles, today)` and `CandidateImport` has four more NOT NULL columns, so Task 0's `run_import` was rewritten; `CandidateShare` and `CandidateAccessRequest` must join `app/models/__init__.py` or a later autogenerate proposes dropping their tables (Tasks 2, 3); the notification **delivery** worker rebuilds the event against `opportunities` and renders from kind-keyed dicts, so Task 6 needed a delivery branch rather than the two-line emit widening it originally claimed; the inbox route registration is now pinned to a line number in `main.py`.

**One ordering hazard.** Task 7 marks two guard assertions `xfail` because they name modules Tasks 10 and 11 create. The marks are `strict=True`, so they fail the suite once those modules exist; Task 11 Step 4 removes them and Task 15 Step 4 greps for survivors. If Task 11 is skipped or reordered, the suite fails loudly rather than silently excusing the guard.
