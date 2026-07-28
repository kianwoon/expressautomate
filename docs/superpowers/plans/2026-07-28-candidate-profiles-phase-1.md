# Candidate Profiles — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each agency a list of the people it places — created and edited by hand, with the identity rules that a later bulk import will depend on.

**Architecture:** One tenant-scoped `candidates` table behind row-level security, with skills and human-edit overrides in their own tables. A matching service resolves a person by email or phone using select-then-write (not upsert — two unique keys cannot share one `ON CONFLICT` arbiter). A read/write API mirrors `app/api/clients.py`, and a Next.js dashboard page mirrors the existing job-orders screens.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Alembic, Postgres 16, pytest-asyncio, `phonenumbers`, Next.js app-router, `uv`.

Spec: [2026-07-28-candidate-profiles-design.md](../specs/2026-07-28-candidate-profiles-design.md)

**Phase 2 (CSV/XLSX bulk import) is a separate plan.** Nothing here may depend on it. Where this plan builds something import will need — the overrides table, the matcher — that is called out.

## Global Constraints

- Backend commands run from `backend/`, prefixed with `uv run`. Frontend commands run from `frontend/` with `npm`.
- **Postgres 16 is required.** The migration chain already uses `NULLS NOT DISTINCT`. Current head is `e5b92d8a7c41` — verify with `uv run alembic heads` before writing a migration rather than trusting this line.
- **No hardcoded values in source.** Every tunable is a field on `app.core.config.settings` (`app/core/config.py`). A literal page size, country code, or phone prefix in a module is a defect.
- **Every tenant-scoped table must `ENABLE` + `FORCE ROW LEVEL SECURITY` and carry the `tenant_isolation` policy in its own migration.** `verify_rls_enforced()` (`app/db/rls.py:58`) discovers tables by catalog query and refuses to boot the app on any readable table missing FORCE.
- **Every foreign key between new tables is composite**, carrying `tenant_id` and referencing `(tenant_id, id)`. RLS does not filter foreign-key validation.
- **All reads and writes go through `tenant_session(tenant_uuid)`** (`app/db/rls.py:35`).
- **Another agency's id returns 404, never 403.**
- Every route lives under `/api`. `tests/test_routing.py` fails if one escapes.
- **Never fabricate a missing value.** Absent data stays NULL.
- Backend line limit 100 chars; `uv run ruff check .` must pass; no source file over 1500 lines.
- Tests run against a local Postgres 16. `tests/conftest.py:44` aborts collection if `DATABASE_URL` or `DATABASE_ADMIN_URL` resolves to a non-local host — never weaken that guard.
- **There is no frontend test framework.** `frontend/package.json` has only `dev`, `build`, `start`, `lint`. Frontend tasks are verified by `npm run lint` and `npm run build` (which type-checks), plus stated manual checks. Do not add a test framework as part of this feature — that is its own decision.

---

### Task 1: Settings and the phone dependency

Everything later depends on these. Doing it first means no task is tempted to inline a country code.

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/pyproject.toml`
- Modify: `.env.example` (repo root)
- Test: `backend/tests/test_candidate_config.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `settings.CANDIDATES_PAGE_LIMIT: int`, `settings.DEFAULT_PHONE_REGION: str`, `settings.MOBILE_PREFIXES: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_candidate_config.py`:

```python
"""Which numbers may identify a person is an operator's judgement, not a literal.

A Singapore office line starts `6` and is shared by everyone at the company, so
matching a candidate on one merges strangers. Which prefixes count as personal
differs by country and changes without a deploy, so it has to be configuration
before the matcher exists to read it.
"""

from app.core.config import settings


def test_candidates_page_limit_is_a_positive_int() -> None:
    assert isinstance(settings.CANDIDATES_PAGE_LIMIT, int)
    assert settings.CANDIDATES_PAGE_LIMIT > 0


def test_default_phone_region_is_a_two_letter_code() -> None:
    assert isinstance(settings.DEFAULT_PHONE_REGION, str)
    assert len(settings.DEFAULT_PHONE_REGION) == 2
    assert settings.DEFAULT_PHONE_REGION.isupper()


def test_mobile_prefixes_are_digits_in_a_frozenset() -> None:
    assert isinstance(settings.MOBILE_PREFIXES, frozenset)
    assert settings.MOBILE_PREFIXES
    assert all(p.isdigit() for p in settings.MOBILE_PREFIXES)


def test_singapore_mobile_prefixes_are_the_default() -> None:
    # 8 and 9 are mobile; 6 is a fixed line and must not be in the set.
    assert {"8", "9"} <= settings.MOBILE_PREFIXES
    assert "6" not in settings.MOBILE_PREFIXES
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
uv run pytest tests/test_candidate_config.py -v
```

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'CANDIDATES_PAGE_LIMIT'`.

- [ ] **Step 3: Add the settings**

In `backend/app/core/config.py`, beside `CLIENTS_PAGE_LIMIT` (around line 292):

```python
    CANDIDATES_PAGE_LIMIT: int = Field(default=200, gt=0)

    # Phone numbers are parsed to E.164 before they identify anyone. A sheet
    # writes "9123 4567" and means +65 9123 4567; without a region there is
    # nothing to resolve that against.
    DEFAULT_PHONE_REGION: str = Field(default="SG", min_length=2, max_length=2)

    # Which leading digits belong to a person rather than a switchboard. A
    # fixed line is shared by a whole company, so matching a candidate on one
    # would merge colleagues into a single record.
    MOBILE_PREFIXES_RAW: str = Field(default="8,9", alias="MOBILE_PREFIXES")

    @property
    def MOBILE_PREFIXES(self) -> frozenset[str]:
        return frozenset(
            part.strip() for part in self.MOBILE_PREFIXES_RAW.split(",") if part.strip()
        )
```

`FREE_EMAIL_DOMAINS_RAW` above it uses exactly this raw-string-plus-property shape with an `alias`, and `model_config` sets `extra="ignore"` with `case_sensitive=True` — follow it rather than inventing a variant.

- [ ] **Step 4: Add the dependency**

```bash
uv add phonenumbers
```

`pyproject.toml` currently has no phone library. `phonenumbers` is Google's libphonenumber port and is what makes `+65 9123 4567`, `6591234567` and `9123-4567` resolve to one canonical string.

- [ ] **Step 5: Add the new keys to `.env.example`**

`tests/test_deployment.py` asserts every setting appears there and will fail otherwise. Add, in the file's existing commented style:

```
# Most candidates one dashboard request returns.
CANDIDATES_PAGE_LIMIT=200
# Region used to parse a phone number that has no country code.
DEFAULT_PHONE_REGION=SG
# Leading digits that mark a personal number. A fixed-line prefix here would
# let one switchboard merge every colleague into a single candidate.
MOBILE_PREFIXES=8,9
```

- [ ] **Step 6: Run the tests**

```bash
uv run pytest tests/test_candidate_config.py tests/test_deployment.py -v && uv run ruff check .
```

Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add app/core/config.py pyproject.toml uv.lock ../.env.example tests/test_candidate_config.py
git commit -m "Make phone identity rules an operator's setting"
```

---

### Task 2: The owner role

Hard delete is owner-only, and **no owner exists today** — `role` defaults to `"recruiter"` (`app/models/tenant.py:51`) and no code ever assigns anything else. Without this task, Task 7's delete endpoint would be unreachable by every user in every tenant.

**Files:**
- Modify: `backend/app/api/auth.py:428-453`
- Create: `backend/alembic/versions/20260728_1500_owner_role.py`
- Test: `backend/tests/test_owner_role.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `users.role` reliably holds `"owner"` for exactly one user per tenant; a CHECK constrains it to `owner | recruiter`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_owner_role.py`:

```python
"""Somebody has to be able to delete a candidate.

`role` has existed since the first migration and has never been read. Phase 1
starts reading it, which turns a dormant column into an access control — so the
values it can hold, and who gets which, stop being cosmetic.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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
        await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def test_role_rejects_a_value_nobody_checks_for(agency) -> None:
    """A typo in a column that gates deletion must fail loudly, not silently."""
    with pytest.raises(IntegrityError):
        async with tenant_session(agency) as s:
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, role) "
                    "VALUES (:i, :t, 'x@a.sg', 'administrator')"
                ),
                {"i": uuid.uuid4(), "t": agency},
            )
            await s.commit()


async def test_owner_and_recruiter_are_both_accepted(agency) -> None:
    async with tenant_session(agency) as s:
        for role in ("owner", "recruiter"):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, role) "
                    "VALUES (:i, :t, :e, :r)"
                ),
                {"i": uuid.uuid4(), "t": agency, "e": f"{role}@a.sg", "r": role},
            )
        await s.commit()
    async with tenant_session(agency) as s:
        roles = sorted((await s.execute(text("SELECT role FROM users"))).scalars().all())
    assert roles == ["owner", "recruiter"]
```

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/test_owner_role.py -v
```

Expected: `test_role_rejects_a_value_nobody_checks_for` FAILS — no CHECK exists, so the insert succeeds.

- [ ] **Step 3: Write the migration**

Run `uv run alembic heads` and use the real head as `down_revision`. Create `backend/alembic/versions/20260728_1500_owner_role.py`:

```python
"""owner role

Revision ID: f1c40a9d5e72
Revises: e5b92d8a7c41
Create Date: 2026-07-28 15:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f1c40a9d5e72'
down_revision: str | None = 'e5b92d8a7c41'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Backfill before constraining: a tenant whose users all predate this
    # change has no owner, and its personal-data deletion would be
    # unreachable — the exact failure this task exists to prevent.
    op.execute(
        """
        UPDATE users SET role = 'owner'
        WHERE id IN (
            SELECT DISTINCT ON (tenant_id) id
            FROM users
            ORDER BY tenant_id, created_at ASC, id ASC
        )
        """
    )
    # Any value that is neither is a typo. Left unconstrained it would deny
    # access silently, which reads as "the button is broken".
    op.execute("UPDATE users SET role = 'recruiter' WHERE role NOT IN ('owner', 'recruiter')")
    op.create_check_constraint(
        'ck_users_role', 'users', "role IN ('owner', 'recruiter')"
    )


def downgrade() -> None:
    op.drop_constraint('ck_users_role', 'users', type_='check')
```

`created_at, id` orders the tie deterministically — two users created in the same transaction share a timestamp, and a non-deterministic backfill would give different tenants different owners on a re-run.

- [ ] **Step 4: Apply it and run the tests**

```bash
uv run alembic upgrade head && uv run pytest tests/test_owner_role.py -v
```

Expected: both pass.

- [ ] **Step 5: Make the first user of a tenant an owner**

Read `app/api/auth.py:428-453`. The user insert is a `pg_insert(...).on_conflict_do_update(...)` on `uq_users_tenant_ms_object_id` with `role="recruiter"` hardcoded at line 435.

Replace that literal with a value computed just above the insert, inside the same session:

```python
        # The person who signs in first is the one who set the agency up, and
        # is the only one who can delete a candidate. Computed inside this
        # transaction so two simultaneous first sign-ins cannot both win.
        existing = (
            await session.execute(
                text("SELECT 1 FROM users WHERE tenant_id = :t LIMIT 1 FOR UPDATE"),
                {"t": tenant_id},
            )
        ).first()
        role = "recruiter" if existing else "owner"
```

and pass `role=role` in the insert values. **Do not** change the `on_conflict_do_update` clause to update `role` — a returning user must keep the role they have, and an owner who signs in again must not be demoted.

- [ ] **Step 6: Add the provisioning tests**

Append to `backend/tests/test_owner_role.py`:

```python
async def test_the_first_user_of_a_tenant_becomes_the_owner(agency) -> None:
    """Follow the real sign-in path — do not insert users directly here.

    Build the two calls the way tests/test_auth.py already drives a sign-in,
    reusing its helpers rather than writing a second definition of the flow.
    """
    first = await _sign_in_new_user(agency, oid="oid-first")
    second = await _sign_in_new_user(agency, oid="oid-second")

    async with tenant_session(agency) as s:
        roles = dict(
            (await s.execute(text("SELECT id, role FROM users"))).all()
        )
    assert roles[first] == "owner"
    assert roles[second] == "recruiter"


async def test_an_owner_signing_in_again_is_not_demoted(agency) -> None:
    uid = await _sign_in_new_user(agency, oid="oid-first")
    await _sign_in_new_user(agency, oid="oid-second")
    await _sign_in_new_user(agency, oid="oid-first")  # same person returns

    async with tenant_session(agency) as s:
        role = (
            await s.execute(text("SELECT role FROM users WHERE id = :i"), {"i": uid})
        ).scalar_one()
    assert role == "owner"
```

`_sign_in_new_user` must be built from the existing helpers in `tests/test_auth.py` — read that file and reuse how it constructs a signed-in user. Do not invent a parallel definition of the auth flow; a second definition will drift from the real one.

- [ ] **Step 7: Run everything**

```bash
uv run pytest tests/test_owner_role.py tests/test_auth.py -v && uv run ruff check .
```

Expected: all passed. `test_auth.py` must be unchanged and still green — this task must not alter who can sign in.

- [ ] **Step 8: Commit**

```bash
git add app/api/auth.py alembic/versions/20260728_1500_owner_role.py tests/test_owner_role.py
git commit -m "Give every agency exactly one owner, so deletion has an owner to require"
```

---

### Task 3: Tables, RLS, and the cross-tenant foreign key

**Files:**
- Create: `backend/app/models/candidate.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/alembic/versions/20260728_1600_candidate_profiles.py`
- Test: `backend/tests/test_candidate_isolation.py` (create)

**Interfaces:**
- Consumes: `TenantScoped`, `UUIDPrimaryKey`, `Timestamps` from `app.db.base`.
- Produces: `app.models.Candidate` (table `candidates`), `CandidateSkill` (`candidate_skills`), `CandidateFieldOverride` (`candidate_field_overrides`); constants `Candidate.ACTIVE`, `ARCHIVED`, `MERGED` and `Candidate.STAGES`.

- [ ] **Step 1: Write the failing isolation test**

Create `backend/tests/test_candidate_isolation.py`:

```python
"""Agency A must never reach agency B's candidates — including by foreign key.

RLS filters what a statement may SELECT and INSERT. It does not filter the
internal referential-integrity check behind a foreign key, so a skill row in
agency A can name agency B's candidate and Postgres accepts it, silently
attaching one agency's data to another's person. Only a composite FK carrying
tenant_id closes that.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from tests.conftest import AdminSessionLocal

_INSERT = (
    "INSERT INTO candidates (id, tenant_id, full_name, email, record_status, "
    "pipeline_stage) VALUES (:i, :t, 'Jane Tan', :e, 'active', 'new')"
)


@pytest.fixture
async def two_agencies():
    a, b = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        for tid in (a, b):
            await s.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
                {"i": tid, "n": f"agency-{tid.hex[:6]}"},
            )
        await s.commit()
    yield a, b
    async with AdminSessionLocal() as s:
        for tid in (a, b):
            for table in (
                "candidate_field_overrides",
                "candidate_skills",
                "candidates",
                "users",
            ):
                await s.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid}
                )
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def test_one_agency_cannot_read_anothers_candidates(two_agencies) -> None:
    a, b = two_agencies
    async with tenant_session(a) as s:
        await s.execute(_INSERT, {"i": uuid.uuid4(), "t": a, "e": "jane@acme.sg"})
        await s.commit()
    async with tenant_session(b) as s:
        assert (await s.execute(text("SELECT id FROM candidates"))).all() == []


async def test_a_skill_cannot_reference_another_agencys_candidate(two_agencies) -> None:
    a, b = two_agencies
    cid = uuid.uuid4()
    async with tenant_session(a) as s:
        await s.execute(_INSERT, {"i": cid, "t": a, "e": "jane@acme.sg"})
        await s.commit()

    with pytest.raises(IntegrityError):
        async with tenant_session(b) as s:
            await s.execute(
                text(
                    "INSERT INTO candidate_skills "
                    "(id, tenant_id, candidate_id, skill, skill_normalized) "
                    "VALUES (:i, :t, :c, 'Python', 'python')"
                ),
                {"i": uuid.uuid4(), "t": b, "c": cid},
            )
            await s.commit()


async def test_an_override_cannot_reference_another_agencys_candidate(two_agencies) -> None:
    a, b = two_agencies
    cid = uuid.uuid4()
    async with tenant_session(a) as s:
        await s.execute(_INSERT, {"i": cid, "t": a, "e": "jane@acme.sg"})
        await s.commit()

    with pytest.raises(IntegrityError):
        async with tenant_session(b) as s:
            await s.execute(
                text(
                    "INSERT INTO candidate_field_overrides "
                    "(id, tenant_id, candidate_id, field_name, human_value) "
                    "VALUES (:i, :t, :c, 'full_name', 'Someone Else')"
                ),
                {"i": uuid.uuid4(), "t": b, "c": cid},
            )
            await s.commit()
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
uv run pytest tests/test_candidate_isolation.py -v
```

Expected: FAIL — `UndefinedTableError: relation "candidates" does not exist`.

- [ ] **Step 3: Write the models**

Create `backend/app/models/candidate.py`:

```python
"""One person an agency places.

Unlike a client, a candidate is never proposed by the pipeline. Email carries
job orders, not CVs — the classifier is binary (`ingest/classify.py:28`) and
drops anything that is not a job order before extraction runs, and attachments
are never downloaded. So every value here has a human author, which is a
stronger provenance claim than any extraction makes and is why none of the
evidence or confidence machinery appears.

Identity is email or phone, either alone. The common case is a recruiter's
older sheet carrying a personal address and the newer one a work address, with
the mobile unchanged; requiring both to agree would duplicate the person every
time. Name is never a key — two different people share a name far more often
than intuition suggests, and merging two real people is worse than a duplicate
somebody can merge later.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Candidate(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "candidates"

    ACTIVE = "active"
    ARCHIVED = "archived"
    MERGED = "merged"
    STAGES = ("new", "contacted", "submitted", "placed", "rejected")

    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    # Stored twice on purpose. `phone_raw` is what the recruiter typed and what
    # they recognise; `phone_e164` is the only form two rows can be compared
    # on. Same raw-beside-normalised rule `opportunities` follows.
    phone_raw: Mapped[str | None] = mapped_column(String(64))
    phone_e164: Mapped[str | None] = mapped_column(String(32))

    current_title: Mapped[str | None] = mapped_column(Text)
    current_employer: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    years_experience: Mapped[int | None] = mapped_column(Integer)
    expected_salary: Mapped[float | None] = mapped_column(Numeric(12, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    # Without the period a monthly and an annual figure average into nonsense
    # — see the comment on `opportunities.salary_period`.
    salary_period: Mapped[str | None] = mapped_column(String(16))
    available_from: Mapped[date | None] = mapped_column(Date)
    notice_period_raw: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(String(32))

    notes: Mapped[str | None] = mapped_column(Text)

    # Where the person is in the process, and whether the row is still real.
    # Separate columns because they answer different questions: collapsing them
    # means archiving somebody destroys the fact that they were placed.
    pipeline_stage: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", index=True
    )
    record_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ACTIVE, index=True
    )
    merged_into_candidate_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_candidates_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "merged_into_candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidates_merged_into_same_tenant",
            ondelete="SET NULL",
        ),
        # Declared here as well as in the migration so autogenerate does not
        # propose dropping them. `merged` is excluded so a merge frees both
        # keys for the surviving row; `archived` stays inside, because an
        # archived person still holds their identity and an import that
        # skipped them would collide on insert instead.
        Index(
            "uq_candidates_tenant_email",
            "tenant_id",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("email IS NOT NULL AND record_status <> 'merged'"),
        ),
        Index(
            "uq_candidates_tenant_phone",
            "tenant_id",
            "phone_e164",
            unique=True,
            postgresql_where=text("phone_e164 IS NOT NULL AND record_status <> 'merged'"),
        ),
    )


class CandidateSkill(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A skill is a row, not an array element, because it is searched on."""

    __tablename__ = "candidate_skills"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    skill: Mapped[str] = mapped_column(Text, nullable=False)
    skill_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_skills_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "skill_normalized",
            name="uq_candidate_skills_once_per_candidate",
        ),
    )


class CandidateFieldOverride(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A field a person edited, which no import may overwrite.

    The shape is borrowed from `opportunity_field_overrides`, but that table is
    a model and nothing else — nothing reads or writes it. There is no working
    implementation to copy, so this is new machinery. The justification differs
    too: there it guards against an AI re-extraction clobbering a human, here
    the only thing that would overwrite is a later import of a stale sheet.
    """

    __tablename__ = "candidate_field_overrides"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    human_value: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_overrides_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "field_name",
            name="uq_candidate_overrides_one_per_field",
        ),
    )
```

- [ ] **Step 4: Register the models**

In `backend/app/models/__init__.py` add the import and the three names to `__all__` in alphabetical position:

```python
from app.models.candidate import Candidate, CandidateFieldOverride, CandidateSkill
```

- [ ] **Step 5: Write the migration**

Create `backend/alembic/versions/20260728_1600_candidate_profiles.py`, with `down_revision` set to Task 2's revision:

```python
"""candidate profiles

Revision ID: a2d71b8c4f39
Revises: f1c40a9d5e72
Create Date: 2026-07-28 16:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a2d71b8c4f39'
down_revision: str | None = 'f1c40a9d5e72'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED: list[tuple[str, str]] = [
    ("candidates", "tenant_id"),
    ("candidate_skills", "tenant_id"),
    ("candidate_field_overrides", "tenant_id"),
]

SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        'candidates',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('full_name', sa.Text(), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=True),
        sa.Column('phone_raw', sa.String(length=64), nullable=True),
        sa.Column('phone_e164', sa.String(length=32), nullable=True),
        sa.Column('current_title', sa.Text(), nullable=True),
        sa.Column('current_employer', sa.Text(), nullable=True),
        sa.Column('location', sa.Text(), nullable=True),
        sa.Column('years_experience', sa.Integer(), nullable=True),
        sa.Column('expected_salary', sa.Numeric(12, 2), nullable=True),
        sa.Column('salary_currency', sa.String(length=8), nullable=True),
        sa.Column('salary_period', sa.String(length=16), nullable=True),
        sa.Column('available_from', sa.Date(), nullable=True),
        sa.Column('notice_period_raw', sa.Text(), nullable=True),
        sa.Column('employment_type', sa.String(length=32), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('pipeline_stage', sa.String(length=16), nullable=False, server_default='new'),
        sa.Column('record_status', sa.String(length=16), nullable=False, server_default='active'),
        sa.Column('merged_into_candidate_id', sa.UUID(), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('updated_by', sa.UUID(), nullable=True),
        sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ondelete='SET NULL'),
        sa.UniqueConstraint('tenant_id', 'id', name='uq_candidates_tenant_id_id'),
        sa.CheckConstraint(
            "(record_status = 'merged') = (merged_into_candidate_id IS NOT NULL)",
            name='ck_candidates_merged_has_target',
        ),
        sa.CheckConstraint(
            "record_status IN ('active', 'archived', 'merged')",
            name='ck_candidates_record_status',
        ),
        sa.CheckConstraint(
            "pipeline_stage IN ('new', 'contacted', 'submitted', 'placed', 'rejected')",
            name='ck_candidates_pipeline_stage',
        ),
        # A person needs a name. Everything else is optional because a
        # recruiter often has a name and a number and nothing more.
        sa.CheckConstraint("length(btrim(full_name)) > 0", name='ck_candidates_name_not_blank'),
    )
    op.create_index(op.f('ix_candidates_tenant_id'), 'candidates', ['tenant_id'])
    op.create_index(op.f('ix_candidates_pipeline_stage'), 'candidates', ['pipeline_stage'])
    op.create_index(op.f('ix_candidates_record_status'), 'candidates', ['record_status'])
    op.create_foreign_key(
        'fk_candidates_merged_into_same_tenant',
        'candidates',
        'candidates',
        ['tenant_id', 'merged_into_candidate_id'],
        ['tenant_id', 'id'],
        ondelete='SET NULL',
    )
    # Email is matched case-insensitively, so the index must be on lower(email)
    # — a plain index would let Jane@acme.sg and jane@acme.sg both exist and
    # then match the same row unpredictably.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_candidates_tenant_email
        ON candidates (tenant_id, lower(email))
        WHERE email IS NOT NULL AND record_status <> 'merged'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_candidates_tenant_phone
        ON candidates (tenant_id, phone_e164)
        WHERE phone_e164 IS NOT NULL AND record_status <> 'merged'
        """
    )

    op.create_table(
        'candidate_skills',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('candidate_id', sa.UUID(), nullable=False),
        sa.Column('skill', sa.Text(), nullable=False),
        sa.Column('skill_normalized', sa.Text(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'candidate_id'],
            ['candidates.tenant_id', 'candidates.id'],
            name='fk_candidate_skills_candidate_same_tenant',
            ondelete='CASCADE',
        ),
        sa.UniqueConstraint(
            'tenant_id', 'candidate_id', 'skill_normalized',
            name='uq_candidate_skills_once_per_candidate',
        ),
    )
    op.create_index(op.f('ix_candidate_skills_tenant_id'), 'candidate_skills', ['tenant_id'])
    op.create_index(op.f('ix_candidate_skills_candidate_id'), 'candidate_skills', ['candidate_id'])
    op.create_index(
        op.f('ix_candidate_skills_skill_normalized'), 'candidate_skills', ['skill_normalized']
    )

    op.create_table(
        'candidate_field_overrides',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('candidate_id', sa.UUID(), nullable=False),
        sa.Column('field_name', sa.String(length=64), nullable=False),
        sa.Column('human_value', sa.Text(), nullable=True),
        sa.Column('changed_by', sa.UUID(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'candidate_id'],
            ['candidates.tenant_id', 'candidates.id'],
            name='fk_candidate_overrides_candidate_same_tenant',
            ondelete='CASCADE',
        ),
        sa.UniqueConstraint(
            'tenant_id', 'candidate_id', 'field_name',
            name='uq_candidate_overrides_one_per_field',
        ),
    )
    op.create_index(
        op.f('ix_candidate_field_overrides_tenant_id'), 'candidate_field_overrides', ['tenant_id']
    )
    op.create_index(
        op.f('ix_candidate_field_overrides_candidate_id'),
        'candidate_field_overrides',
        ['candidate_id'],
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
    """FORCE, not merely ENABLE: without it the table owner bypasses the
    policy, and the owner is who migrations and any superuser session connect
    as — so an ENABLE-only table looks protected in the catalogue and is not.
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
    op.drop_table('candidate_field_overrides')
    op.drop_table('candidate_skills')
    op.execute("DROP INDEX IF EXISTS uq_candidates_tenant_phone")
    op.execute("DROP INDEX IF EXISTS uq_candidates_tenant_email")
    op.drop_table('candidates')
```

- [ ] **Step 6: Apply and test**

```bash
uv run alembic upgrade head && uv run pytest tests/test_candidate_isolation.py tests/test_guards.py -v
```

Expected: all passed. If a cross-tenant test passes for a reason other than the composite FK, check the error names the constraint — `fk_candidate_skills_candidate_same_tenant`.

`test_guards.py` exercises `verify_rls_enforced()`; a table missing FORCE fails here rather than in production.

- [ ] **Step 7: Confirm no model drift and that it reverses**

```bash
uv run alembic check && uv run alembic downgrade -1 && uv run alembic upgrade head
```

Expected: "No new upgrade operations detected", then both migrations run. `alembic check` catches an index declared in one place and not the other — the client feature shipped that bug and it would have made a later autogenerate propose dropping the identity key.

- [ ] **Step 8: Commit**

```bash
git add app/models/candidate.py app/models/__init__.py alembic/versions/20260728_1600_candidate_profiles.py tests/test_candidate_isolation.py
git commit -m "Give candidates a table one agency cannot reach from another"
```

---

### Task 4: Phone normalization

Small and pure, isolated so the matcher's tests need not relitigate string handling.

**Files:**
- Create: `backend/app/services/candidate_naming.py`
- Test: `backend/tests/test_candidate_naming.py` (create)

**Interfaces:**
- Consumes: `settings.DEFAULT_PHONE_REGION`, `settings.MOBILE_PREFIXES` (Task 1).
- Produces: `normalize_phone(raw: str | None) -> str | None`, `is_matchable_phone(e164: str | None) -> bool`, `normalize_email(raw: str | None) -> str | None`, `normalize_skill(raw: str) -> str`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_candidate_naming.py`:

```python
"""What may identify a person, and what may not.

Every function is total: the pipeline genuinely produces blanks and rubbish,
and a normaliser that raised would fail an entire import over one malformed
cell. `None` means "no usable key", which sends the caller to a different
strategy rather than inventing one.
"""

import pytest

from app.services.candidate_naming import (
    is_matchable_phone,
    normalize_email,
    normalize_phone,
    normalize_skill,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+65 9123 4567", "+6591234567"),
        ("9123 4567", "+6591234567"),      # bare local number, default region
        ("9123-4567", "+6591234567"),
        ("6591234567", "+6591234567"),
        ("+65 6123 4567", "+6561234567"),  # office line: parses fine
        ("", None),
        (None, None),
        ("not a phone", None),
        ("12", None),                       # too short to be anyone's number
    ],
)
def test_normalize_phone(raw: str | None, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("e164", "matchable"),
    [
        ("+6591234567", True),    # mobile
        ("+6581234567", True),    # mobile
        ("+6561234567", False),   # fixed line — shared by a whole company
        (None, False),
    ],
)
def test_only_personal_numbers_may_identify_someone(e164: str | None, matchable: bool) -> None:
    assert is_matchable_phone(e164) is matchable


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jane@Acme.SG", "jane@acme.sg"),
        ("  jane@acme.sg  ", "jane@acme.sg"),
        ("Jane Tan <jane@acme.sg>", "jane@acme.sg"),
        ("not-an-email", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_email(raw: str | None, expected: str | None) -> None:
    assert normalize_email(raw) == expected


def test_normalize_skill_folds_case_and_space() -> None:
    assert normalize_skill("  Python  3 ") == "python 3"
    assert normalize_skill("PYTHON") == "python"
```

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/test_candidate_naming.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.candidate_naming'`.

- [ ] **Step 3: Implement**

Create `backend/app/services/candidate_naming.py`:

```python
"""Turning what a recruiter typed into something two rows can be compared on.

Total and pure. A blank cell, a phone number with a typo, and a name in an
angle-bracketed header are all normal input here, and each returns a value
rather than raising.
"""

import re

import phonenumbers

from app.core.config import settings

_WHITESPACE = re.compile(r"\s+")
_ANGLE = re.compile(r"<([^>]+)>")


def normalize_phone(raw: str | None) -> str | None:
    """E.164, or None when the number cannot be parsed confidently.

    None is the honest answer for rubbish. A half-parsed number used as an
    identity key is worse than none at all: it silently splits one person into
    two records, or merges two people into one.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = phonenumbers.parse(raw, settings.DEFAULT_PHONE_REGION)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def is_matchable_phone(e164: str | None) -> bool:
    """Whether this number identifies a person rather than a switchboard.

    A fixed line belongs to a company, so matching a candidate on one would
    merge every colleague who ever listed the office number into one record.
    Such a number is still stored and still displayed — it simply never
    decides that two rows are the same person.
    """
    if not e164:
        return False
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return False
    national = str(parsed.national_number)
    return bool(national) and national[0] in settings.MOBILE_PREFIXES


def normalize_email(raw: str | None) -> str | None:
    """Lowercased address, or None if there isn't one.

    Handles the angle-bracket header form, because a pasted address often
    arrives as `Jane Tan <jane@acme.sg>` and storing that whole string as an
    identity key would make the same person fail to match themselves.
    """
    if not raw:
        return None
    text = raw.strip()
    match = _ANGLE.search(text)
    if match:
        text = match.group(1).strip()
    if text.count("@") != 1:
        return None
    local, _, domain = text.partition("@")
    if not local or not domain or "." not in domain:
        return None
    return f"{local}@{domain}".lower()


def normalize_skill(raw: str) -> str:
    """Lowercase, collapse whitespace. Deliberately blunt.

    A cleverer normaliser that stemmed or aliased would make "Java" and
    "JavaScript" collide, which is worse than two rows a recruiter can read.
    """
    return _WHITESPACE.sub(" ", raw.lower()).strip()
```

- [ ] **Step 4: Run it**

```bash
uv run pytest tests/test_candidate_naming.py -v && uv run ruff check .
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/candidate_naming.py tests/test_candidate_naming.py
git commit -m "Compare people on keys that cannot merge two of them"
```

---

### Task 5: The matcher

**Files:**
- Create: `backend/app/services/candidate_matching.py`
- Test: `backend/tests/test_candidate_matching.py` (create)

**Interfaces:**
- Consumes: `normalize_email`, `normalize_phone`, `is_matchable_phone` (Task 4); `Candidate` (Task 3).
- Produces: `async def find_candidate(session, tenant_id, email, phone_e164) -> MatchResult`, where `MatchResult` is a dataclass with `candidate_id: uuid.UUID | None`, `matched_on: str | None` (`"email"` / `"phone"`), and `conflict: tuple[uuid.UUID, uuid.UUID] | None`. **Takes an existing session** — it must run inside the caller's transaction.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_candidate_matching.py`:

```python
"""Which existing person a set of details refers to, if any.

The matcher only ever *reads*. It reports what it found — including that it
found two different people — and the caller decides what to write. That split
is what makes the same function usable from a manual POST and, later, from a
bulk import that must record an outcome per row.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.candidate_matching import find_candidate
from tests.conftest import AdminSessionLocal

_INSERT = (
    "INSERT INTO candidates (id, tenant_id, full_name, email, phone_e164, "
    "record_status, pipeline_stage) "
    "VALUES (:i, :t, :n, :e, :p, :s, 'new')"
)


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
        # Clear the merge pointers before deleting: the CHECK requires a merged
        # row to name a target, so status and target must be cleared together,
        # and the self-FK blocks deleting a target while a loser points at it.
        await s.execute(
            text(
                "UPDATE candidates SET record_status = 'active', "
                "merged_into_candidate_id = NULL WHERE tenant_id = :t"
            ),
            {"t": tid},
        )
        for table in ("candidate_field_overrides", "candidate_skills", "candidates"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _seed(tenant_id, *, name="Jane Tan", email=None, phone=None, status="active"):
    cid = uuid.uuid4()
    async with tenant_session(tenant_id) as s:
        await s.execute(
            _INSERT,
            {"i": cid, "t": tenant_id, "n": name, "e": email, "p": phone, "s": status},
        )
        await s.commit()
    return cid


async def test_email_alone_matches(agency) -> None:
    cid = await _seed(agency, email="jane@acme.sg")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "jane@acme.sg", None)
    assert result.candidate_id == cid
    assert result.matched_on == "email"


async def test_phone_alone_matches(agency) -> None:
    cid = await _seed(agency, phone="+6591234567")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, None, "+6591234567")
    assert result.candidate_id == cid
    assert result.matched_on == "phone"


async def test_a_changed_email_still_matches_on_the_unchanged_mobile(agency) -> None:
    """The case the whole either-key rule exists for."""
    cid = await _seed(agency, email="jane@gmail.com", phone="+6591234567")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "jane.tan@newco.sg", "+6591234567")
    assert result.candidate_id == cid


async def test_email_is_matched_case_insensitively(agency) -> None:
    cid = await _seed(agency, email="jane@acme.sg")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "JANE@ACME.SG", None)
    assert result.candidate_id == cid


async def test_nothing_to_match_on_finds_nobody(agency) -> None:
    await _seed(agency, email="jane@acme.sg")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, None, None)
    assert result.candidate_id is None
    assert result.conflict is None


async def test_a_split_identity_is_a_conflict_not_a_guess(agency) -> None:
    """Email says one person, phone says another. Both answers would be wrong."""
    a = await _seed(agency, name="Jane Tan", email="jane@acme.sg")
    b = await _seed(agency, name="John Lim", phone="+6591234567")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "jane@acme.sg", "+6591234567")
    assert result.candidate_id is None
    assert result.conflict is not None
    assert set(result.conflict) == {a, b}


async def test_an_archived_candidate_still_matches(agency) -> None:
    """They still hold the unique key; skipping them would collide on insert."""
    cid = await _seed(agency, email="jane@acme.sg", status="archived")
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "jane@acme.sg", None)
    assert result.candidate_id == cid


async def test_a_merged_candidate_is_not_returned(agency) -> None:
    """A merged row's identity belongs to its target, so it is not a match."""
    survivor = await _seed(agency, email="survivor@acme.sg")
    loser = await _seed(agency, email="loser@acme.sg")
    async with tenant_session(agency) as s:
        await s.execute(
            text(
                "UPDATE candidates SET record_status = 'merged', "
                "merged_into_candidate_id = :w WHERE id = :l"
            ),
            {"w": survivor, "l": loser},
        )
        await s.commit()
    async with tenant_session(agency) as s:
        result = await find_candidate(s, agency, "loser@acme.sg", None)
    assert result.candidate_id is None
```

- [ ] **Step 2: Run them**

```bash
uv run pytest tests/test_candidate_matching.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.candidate_matching'`.

- [ ] **Step 3: Implement**

Create `backend/app/services/candidate_matching.py`:

```python
"""Deciding which existing person a set of details refers to.

Read-only, and deliberately so. It reports what it found and never writes, so
the manual create path and the later bulk import can share it while recording
completely different things about the outcome.

**This is not the client matcher's shape, and copying that would be a bug.**
`client_matching.py` resolves its race with `INSERT ... ON CONFLICT`, which
works because a client has exactly one identity key, so the statement can name
one arbiter index. Postgres allows only one arbiter. A candidate has two keys,
and a row that collides on the key the clause did not name raises a unique
violation the statement cannot absorb. So this is select-then-write, with the
unique indexes as the backstop against a race rather than the mechanism.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.candidate_naming import is_matchable_phone, normalize_email

# `merged` rows are excluded: their identity now belongs to the target. Archived
# rows are NOT excluded — they still hold the unique key, so skipping one would
# send the caller to an insert that collides with it.
_BY_EMAIL = text(
    """
    SELECT id FROM candidates
    WHERE lower(email) = :email AND record_status <> 'merged'
    LIMIT 1
    """
)

_BY_PHONE = text(
    """
    SELECT id FROM candidates
    WHERE phone_e164 = :phone AND record_status <> 'merged'
    LIMIT 1
    """
)


@dataclass(frozen=True)
class MatchResult:
    """What the matcher found. Exactly one of these situations holds.

    - `candidate_id` set: one person. `matched_on` says which key decided.
    - `conflict` set: two different people. The caller must not pick one.
    - both None: nobody matched, so this is somebody new.
    """

    candidate_id: uuid.UUID | None = None
    matched_on: str | None = None
    conflict: tuple[uuid.UUID, uuid.UUID] | None = None


async def find_candidate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    email: str | None,
    phone_e164: str | None,
) -> MatchResult:
    """Resolve these details to an existing candidate, if there is one.

    Runs on the caller's session so the lookup and whatever it decides to write
    share one transaction. A separate connection would let a candidate be
    created against a match that was rolled back.
    """
    normalized_email = normalize_email(email)
    # A fixed line identifies a company. Kept on the record, never used to
    # decide that two rows are the same person.
    usable_phone = phone_e164 if is_matchable_phone(phone_e164) else None

    by_email: uuid.UUID | None = None
    by_phone: uuid.UUID | None = None

    if normalized_email:
        row = (await session.execute(_BY_EMAIL, {"email": normalized_email})).first()
        by_email = row.id if row else None

    if usable_phone:
        row = (await session.execute(_BY_PHONE, {"phone": usable_phone})).first()
        by_phone = row.id if row else None

    # Two keys pointing at two different people. Picking either would attach
    # one person's details to the other's record; creating a third would
    # silently duplicate both. Neither is a decision code should make.
    if by_email and by_phone and by_email != by_phone:
        return MatchResult(conflict=(by_email, by_phone))

    if by_email:
        return MatchResult(candidate_id=by_email, matched_on="email")
    if by_phone:
        return MatchResult(candidate_id=by_phone, matched_on="phone")
    return MatchResult()
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_candidate_matching.py -v && uv run ruff check .
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/candidate_matching.py tests/test_candidate_matching.py
git commit -m "Report which person these details name, or that two of them do"
```

---

### Task 6: Read API

**Files:**
- Create: `backend/app/api/candidates.py`
- Modify: `backend/app/main.py` (import and `include_router`)
- Test: `backend/tests/test_candidates_api.py` (create)

**Interfaces:**
- Consumes: `Candidate`, `CandidateSkill`, `CandidateFieldOverride` (Task 3); `settings.CANDIDATES_PAGE_LIMIT` (Task 1); `_require_session` from `app.api.auth`.
- Produces: `GET /api/candidates`, `GET /api/candidates/{id}`; the module-level helpers `_load(session, candidate_id) -> Candidate` and `_serialize(candidate) -> dict` that Task 7 extends.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_candidates_api.py`. Reuse the sign-in helper and fixture style from `tests/test_clients_api.py` rather than writing new ones — read that file first.

```python
"""The candidate list, as a recruiter reads it.

Counts are computed over the whole tenant rather than the page: a chip that
shrank as you paged would answer a different question than it appears to.
Merged rows are hidden by default because a merged row is not a person any
more, but stay reachable by id so an unmerge is still possible.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.main import app
from tests.conftest import AdminSessionLocal
from tests.test_clients_api import sign_in  # the real session cookie, not a copy


@pytest.fixture
async def agency_with_candidates():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    ids = {"active": uuid.uuid4(), "placed": uuid.uuid4(), "merged": uuid.uuid4()}
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        rows = [
            (ids["active"], "Jane Tan", "jane@acme.sg", "new", "active", None),
            (ids["placed"], "John Lim", "john@acme.sg", "placed", "active", None),
            (ids["merged"], "Jane T", "jane.t@acme.sg", "new", "merged", ids["active"]),
        ]
        for cid, name, email, stage, status, target in rows:
            await s.execute(
                text(
                    "INSERT INTO candidates (id, tenant_id, full_name, email, "
                    "pipeline_stage, record_status, merged_into_candidate_id) "
                    "VALUES (:i, :t, :n, :e, :st, :rs, :mt)"
                ),
                {"i": cid, "t": tid, "n": name, "e": email, "st": stage,
                 "rs": status, "mt": target},
            )
        await s.commit()
    yield tid, uid, ids
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE candidates SET record_status = 'active', "
                "merged_into_candidate_id = NULL WHERE tenant_id = :t"
            ),
            {"t": tid},
        )
        for table in ("candidate_field_overrides", "candidate_skills", "candidates", "users"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def _client_for(tid, uid) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(c, uid, tid)
    return c


async def test_the_list_hides_merged_rows(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/candidates")).json()
    assert {row["id"] for row in body["items"]} == {str(ids["active"]), str(ids["placed"])}


async def test_the_stage_filter_narrows_the_list(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/candidates?pipeline_stage=placed")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["placed"])]


async def test_counts_are_tenant_wide_not_page_wide(agency_with_candidates) -> None:
    tid, uid, _ = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        first = (await http.get("/api/candidates?limit=1&offset=0")).json()
        second = (await http.get("/api/candidates?limit=1&offset=1")).json()
    assert first["counts"] == second["counts"]
    assert len(first["items"]) == 1


async def test_search_finds_a_candidate_by_email(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get("/api/candidates?q=john@acme.sg")).json()
    assert [row["id"] for row in body["items"]] == [str(ids["placed"])]


async def test_a_merged_candidate_is_still_reachable_by_id(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.get(f"/api/candidates/{ids['merged']}")
    assert r.status_code == 200
    assert r.json()["record_status"] == "merged"


async def test_another_agencys_candidate_is_a_404_not_a_403(agency_with_candidates) -> None:
    """403 would confirm the id exists, which is itself a disclosure."""
    _tid, _uid, ids = agency_with_candidates
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
            r = await http.get(f"/api/candidates/{ids['active']}")
            listing = (await http.get("/api/candidates")).json()
        assert r.status_code == 404
        assert listing["items"] == []
    finally:
        async with AdminSessionLocal() as s:
            await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": other_tid})
            await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": other_tid})
            await s.commit()
```

- [ ] **Step 2: Run them**

```bash
uv run pytest tests/test_candidates_api.py -v
```

Expected: FAIL — 404 on every route.

- [ ] **Step 3: Implement the router**

Create `backend/app/api/candidates.py`:

```python
"""The agency's candidate list.

Nothing here is AI-derived. Every value was typed by a person or came from a
spreadsheet a person uploaded, so there is no confidence, no evidence, and no
review queue — only records and the people who edited them.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, or_, select

from app.api.auth import _require_session
from app.core.config import settings
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateFieldOverride, CandidateSkill

router = APIRouter(tags=["candidates"])

StageFilter = Literal["new", "contacted", "submitted", "placed", "rejected"]


def _serialize(candidate: Candidate) -> dict:
    return {
        "id": str(candidate.id),
        "full_name": candidate.full_name,
        "email": candidate.email,
        "phone_raw": candidate.phone_raw,
        "phone_e164": candidate.phone_e164,
        "current_title": candidate.current_title,
        "current_employer": candidate.current_employer,
        "location": candidate.location,
        "years_experience": candidate.years_experience,
        "expected_salary": (
            float(candidate.expected_salary) if candidate.expected_salary is not None else None
        ),
        "salary_currency": candidate.salary_currency,
        "salary_period": candidate.salary_period,
        "available_from": (
            candidate.available_from.isoformat() if candidate.available_from else None
        ),
        "notice_period_raw": candidate.notice_period_raw,
        "employment_type": candidate.employment_type,
        "notes": candidate.notes,
        "pipeline_stage": candidate.pipeline_stage,
        "record_status": candidate.record_status,
        "merged_into_candidate_id": (
            str(candidate.merged_into_candidate_id)
            if candidate.merged_into_candidate_id
            else None
        ),
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
    }


@router.get("/candidates")
async def list_candidates(
    request: Request,
    # Resolved in the body, not the signature: a default bound at import would
    # freeze the setting at the value it had when the module loaded.
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    pipeline_stage: StageFilter | None = None,
    q: str | None = None,
) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    ceiling = settings.CANDIDATES_PAGE_LIMIT
    page_limit = ceiling if limit is None else min(limit, ceiling)

    async with tenant_session(tenant_uuid) as session:
        # Counted over the whole tenant, before any filter or window, so a
        # chip does not change meaning as the recruiter pages.
        counts = {"all": 0}
        for stage, n in await session.execute(
            select(Candidate.pipeline_stage, func.count())
            .where(Candidate.record_status != Candidate.MERGED)
            .group_by(Candidate.pipeline_stage)
        ):
            counts["all"] += n
            counts[stage] = counts.get(stage, 0) + n

        base = select(Candidate).where(Candidate.record_status != Candidate.MERGED)
        if pipeline_stage is not None:
            base = base.where(Candidate.pipeline_stage == pipeline_stage)
        if q:
            # Name, email and phone: the three things a recruiter has to hand
            # when they are looking for somebody they spoke to last week.
            like = f"%{q.strip().lower()}%"
            base = base.where(
                or_(
                    func.lower(Candidate.full_name).like(like),
                    func.lower(Candidate.email).like(like),
                    Candidate.phone_e164.like(like),
                    Candidate.phone_raw.like(like),
                )
            )

        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = (
            await session.execute(
                base.order_by(Candidate.updated_at.desc()).limit(page_limit).offset(offset)
            )
        ).scalars().all()

    return {
        "items": [_serialize(c) for c in rows],
        "total": total,
        "limit": page_limit,
        "offset": offset,
        "counts": counts,
    }


@router.get("/candidates/{candidate_id}")
async def get_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        skills = (
            await session.execute(
                select(CandidateSkill)
                .where(CandidateSkill.candidate_id == candidate_id)
                .order_by(CandidateSkill.skill_normalized)
            )
        ).scalars().all()
        overrides = (
            await session.execute(
                select(CandidateFieldOverride.field_name).where(
                    CandidateFieldOverride.candidate_id == candidate_id
                )
            )
        ).scalars().all()

    payload = _serialize(candidate)
    payload["skills"] = [s.skill for s in skills]
    # So the UI can say why an import did not change a field, rather than
    # leaving the recruiter to conclude the import is broken.
    payload["overridden_fields"] = sorted(overrides)
    return payload


async def _load(session, candidate_id: uuid.UUID) -> Candidate:
    """Fetch inside the tenant session, so another agency's id is a 404.

    Not a 403: telling a caller that an id exists but is not theirs is itself
    a cross-tenant disclosure.
    """
    candidate = (
        await session.execute(select(Candidate).where(Candidate.id == candidate_id))
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate
```

- [ ] **Step 4: Register the router**

In `backend/app/main.py`, add `candidates` to the `from app.api import (...)` block in alphabetical position, and add beside the other includes:

```python
api.include_router(candidates.router)
```

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/test_candidates_api.py tests/test_routing.py -v && uv run ruff check .
```

Expected: all passed. `test_routing.py` proves no new route escaped `/api`.

- [ ] **Step 6: Commit**

```bash
git add app/api/candidates.py app/main.py tests/test_candidates_api.py
git commit -m "Let a recruiter find a candidate they only half remember"
```

---

### Task 7: Write API — create, edit, archive, merge, delete, export

**Files:**
- Modify: `backend/app/api/candidates.py`
- Test: `backend/tests/test_candidates_api.py` (extend)

**Interfaces:**
- Consumes: `_load`, `_serialize` (Task 6); `find_candidate`, `MatchResult` (Task 5); `normalize_email`, `normalize_phone`, `normalize_skill` (Task 4).
- Produces: `POST /api/candidates`, `PATCH /api/candidates/{id}`, `POST /api/candidates/{id}/archive`, `/merge`, `/unmerge`, `DELETE /api/candidates/{id}`, `GET /api/candidates/{id}/export`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_candidates_api.py`:

```python
async def test_creating_a_candidate_records_who_did_it(agency_with_candidates) -> None:
    tid, uid, _ = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post(
            "/api/candidates",
            json={"full_name": "New Person", "email": "new@acme.sg", "skills": ["Python"]},
        )
    assert r.status_code == 201
    async with AdminSessionLocal() as s:
        created_by = (
            await s.execute(
                text("SELECT created_by FROM candidates WHERE id = :i"),
                {"i": uuid.UUID(r.json()["id"])},
            )
        ).scalar_one()
    assert created_by == uid


async def test_creating_a_duplicate_email_is_a_conflict_not_a_500(agency_with_candidates) -> None:
    tid, uid, _ = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post("/api/candidates", json={"full_name": "X", "email": "jane@acme.sg"})
    assert r.status_code == 409


async def test_a_split_identity_is_refused_with_both_names(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        await http.patch(
            f"/api/candidates/{ids['placed']}", json={"phone_raw": "+65 9123 4567"}
        )
        r = await http.post(
            "/api/candidates",
            json={"full_name": "Z", "email": "jane@acme.sg", "phone_raw": "+65 9123 4567"},
        )
    assert r.status_code == 409
    assert "jane@acme.sg" in r.text or str(ids["active"]) in r.text


async def test_editing_a_field_records_an_override(agency_with_candidates) -> None:
    """This is what stops a later import undoing a recruiter's correction."""
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        await http.patch(
            f"/api/candidates/{ids['active']}", json={"current_title": "Senior Engineer"}
        )
        body = (await http.get(f"/api/candidates/{ids['active']}")).json()
    assert body["current_title"] == "Senior Engineer"
    assert "current_title" in body["overridden_fields"]


async def test_a_recruiter_may_archive_but_not_delete(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with AdminSessionLocal() as s:
        await s.execute(
            text("UPDATE users SET role = 'recruiter' WHERE id = :i"), {"i": uid}
        )
        await s.commit()
    async with await _client_for(tid, uid) as http:
        assert (await http.post(f"/api/candidates/{ids['active']}/archive")).status_code == 200
        assert (await http.delete(f"/api/candidates/{ids['active']}")).status_code == 403


async def test_an_owner_may_delete(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates  # fixture creates this user as owner
    async with await _client_for(tid, uid) as http:
        assert (await http.delete(f"/api/candidates/{ids['placed']}")).status_code == 204
        assert (await http.get(f"/api/candidates/{ids['placed']}")).status_code == 404


async def test_export_returns_every_stored_field(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        body = (await http.get(f"/api/candidates/{ids['active']}/export")).json()
    assert body["email"] == "jane@acme.sg"
    assert "skills" in body


async def test_merge_moves_skills_and_frees_both_keys(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post(
            f"/api/candidates/{ids['placed']}/merge",
            json={"target_id": str(ids["active"])},
        )
        assert r.status_code == 200
        # The loser's email is free again, so a new person may take it.
        created = await http.post(
            "/api/candidates", json={"full_name": "Someone New", "email": "john@acme.sg"}
        )
    assert created.status_code == 201


async def test_a_candidate_cannot_be_merged_into_itself(agency_with_candidates) -> None:
    tid, uid, ids = agency_with_candidates
    async with await _client_for(tid, uid) as http:
        r = await http.post(
            f"/api/candidates/{ids['active']}/merge",
            json={"target_id": str(ids["active"])},
        )
    assert r.status_code == 400
```

- [ ] **Step 2: Run them**

```bash
uv run pytest tests/test_candidates_api.py -v
```

Expected: the new tests FAIL with 405 or 404 — the routes do not exist.

- [ ] **Step 3: Implement the write endpoints**

Add to `backend/app/api/candidates.py`. The new names this step needs, on top of Task 6's imports:

```python
from datetime import date

from fastapi import Response
from pydantic import BaseModel
from sqlalchemy import delete, insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.models.tenant import User
from app.services.candidate_matching import find_candidate
from app.services.candidate_naming import normalize_email, normalize_phone, normalize_skill
```

`select`, `func`, `text` and the model imports are already present from Task 6.

```python
class CandidateIn(BaseModel):
    """Only `full_name` is required.

    A recruiter frequently has a name and a phone number and nothing else, and
    a form that refused that would be a form they work around.
    """

    full_name: str
    email: str | None = None
    phone_raw: str | None = None
    current_title: str | None = None
    current_employer: str | None = None
    location: str | None = None
    years_experience: int | None = None
    expected_salary: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    available_from: date | None = None
    notice_period_raw: str | None = None
    employment_type: str | None = None
    notes: str | None = None
    pipeline_stage: StageFilter | None = None
    skills: list[str] | None = None


class MergeRequest(BaseModel):
    target_id: uuid.UUID


# Fields a human edit protects from a later import. `skills` is excluded: it is
# a set, not a value, and merging an imported skill into a curated list loses
# nothing.
_OVERRIDABLE = (
    "full_name", "email", "phone_raw", "current_title", "current_employer",
    "location", "years_experience", "expected_salary", "salary_currency",
    "salary_period", "available_from", "notice_period_raw", "employment_type",
    "notes",
)


@router.post("/candidates", status_code=201)
async def create_candidate(request: Request, body: CandidateIn) -> dict:
    user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        phone_e164 = normalize_phone(body.phone_raw)
        email = normalize_email(body.email)

        match = await find_candidate(session, tenant_uuid, email, phone_e164)
        if match.conflict is not None:
            # Two different people. Attaching to either would put one person's
            # details on the other's record.
            raise HTTPException(
                status_code=409,
                detail=(
                    "This email and phone belong to two different candidates "
                    f"({match.conflict[0]} and {match.conflict[1]}). "
                    "Merge them first, or correct the details."
                ),
            )
        if match.candidate_id is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Already recorded as candidate {match.candidate_id}",
            )

        candidate_id = uuid.uuid4()
        values = body.model_dump(exclude={"skills"})
        values.update(
            id=candidate_id,
            tenant_id=tenant_uuid,
            email=email,
            phone_e164=phone_e164,
            pipeline_stage=body.pipeline_stage or "new",
            record_status=Candidate.ACTIVE,
            created_by=user_uuid,
            updated_by=user_uuid,
        )
        try:
            await session.execute(insert(Candidate).values(**values))
            await _replace_skills(session, tenant_uuid, candidate_id, body.skills or [])
            await session.commit()
        except IntegrityError as exc:
            # The unique indexes are the backstop for a race the matcher's
            # read could not see. A 409 says the same thing the matcher would
            # have; a 500 would blame the recruiter for a collision.
            await session.rollback()
            raise HTTPException(status_code=409, detail="Already recorded") from exc

    return await get_candidate(request, candidate_id)


@router.patch("/candidates/{candidate_id}")
async def update_candidate(request: Request, candidate_id: uuid.UUID, body: CandidateIn) -> dict:
    user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        await _load(session, candidate_id)
        values = body.model_dump(exclude={"skills"}, exclude_unset=True)
        if "phone_raw" in values:
            values["phone_e164"] = normalize_phone(values["phone_raw"])
        if "email" in values:
            values["email"] = normalize_email(values["email"])
        values["updated_by"] = user_uuid

        await session.execute(
            update(Candidate).where(Candidate.id == candidate_id).values(**values)
        )
        # Every edited field is remembered as a human decision. Without this a
        # later import of a stale sheet silently undoes the correction, and
        # nothing in the data afterwards could say it happened.
        for field in _OVERRIDABLE:
            if field in values:
                await session.execute(
                    pg_insert(CandidateFieldOverride)
                    .values(
                        id=uuid.uuid4(),
                        tenant_id=tenant_uuid,
                        candidate_id=candidate_id,
                        field_name=field,
                        human_value=None if values[field] is None else str(values[field]),
                        changed_by=user_uuid,
                    )
                    .on_conflict_do_update(
                        constraint="uq_candidate_overrides_one_per_field",
                        set_={
                            "human_value": (
                                None if values[field] is None else str(values[field])
                            ),
                            "changed_by": user_uuid,
                        },
                    )
                )
        if body.skills is not None:
            await _replace_skills(session, tenant_uuid, candidate_id, body.skills)
        await session.commit()

    return await get_candidate(request, candidate_id)


@router.post("/candidates/{candidate_id}/archive")
async def archive_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        if candidate.record_status == Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the candidate first")
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(record_status=Candidate.ARCHIVED)
        )
        await session.commit()
    return {"record_status": Candidate.ARCHIVED}


@router.post("/candidates/{candidate_id}/merge")
async def merge_candidate(
    request: Request, candidate_id: uuid.UUID, body: MergeRequest
) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    if body.target_id == candidate_id:
        raise HTTPException(status_code=400, detail="A candidate cannot be merged into itself")

    async with tenant_session(tenant_uuid) as session:
        loser = await _load(session, candidate_id)
        target = await _load(session, body.target_id)
        if target.record_status == Candidate.MERGED:
            # Chains would need every reader to walk them. Refusing here is
            # what keeps the graph one hop deep.
            raise HTTPException(
                status_code=400, detail="Target is itself merged; merge into its target"
            )
        if loser.record_status == Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Candidate is already merged")

        # Skills that the target already has would violate the per-candidate
        # unique key, so move only the ones it lacks and drop the rest — a
        # duplicate skill carries no information the target does not have.
        await session.execute(
            text(
                """
                DELETE FROM candidate_skills loser
                WHERE loser.candidate_id = :loser
                  AND EXISTS (
                      SELECT 1 FROM candidate_skills t
                      WHERE t.candidate_id = :target
                        AND t.skill_normalized = loser.skill_normalized
                  )
                """
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        await session.execute(
            text(
                "UPDATE candidate_skills SET candidate_id = :target WHERE candidate_id = :loser"
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        # Overrides move the same way, and for the same reason: they are a
        # record of what a person decided about this human being.
        await session.execute(
            text(
                """
                DELETE FROM candidate_field_overrides loser
                WHERE loser.candidate_id = :loser
                  AND EXISTS (
                      SELECT 1 FROM candidate_field_overrides t
                      WHERE t.candidate_id = :target AND t.field_name = loser.field_name
                  )
                """
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        await session.execute(
            text(
                "UPDATE candidate_field_overrides SET candidate_id = :target "
                "WHERE candidate_id = :loser"
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        # Status and target in one statement — a CHECK enforces that a merged
        # row names its target and a live row does not.
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(
                record_status=Candidate.MERGED, merged_into_candidate_id=body.target_id
            )
        )
        await session.commit()
    return {"record_status": Candidate.MERGED, "merged_into_candidate_id": str(body.target_id)}


@router.post("/candidates/{candidate_id}/unmerge")
async def unmerge_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    """Restore a merged candidate. Skills and overrides stay with the target.

    Deliberately partial: a moved row carries no record of which candidate it
    came from, so it cannot be given back. The identity keys return, which is
    what makes the person findable again.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        if candidate.record_status != Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Candidate is not merged")

        # Unmerging returns this row's email and phone to the live indexes. If
        # somebody else took either in the meantime, restoring would violate a
        # unique index — so say who holds it rather than 500.
        clash = (
            await session.execute(
                text(
                    """
                    SELECT id, full_name FROM candidates
                    WHERE record_status <> 'merged' AND id <> :id
                      AND ((:email IS NOT NULL AND lower(email) = lower(:email))
                        OR (:phone IS NOT NULL AND phone_e164 = :phone))
                    LIMIT 1
                    """
                ),
                {"id": candidate_id, "email": candidate.email, "phone": candidate.phone_e164},
            )
        ).first()
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot unmerge: {clash.full_name} ({clash.id}) now holds "
                    "this candidate's email or phone."
                ),
            )

        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(record_status=Candidate.ACTIVE, merged_into_candidate_id=None)
        )
        await session.commit()
    return {"record_status": Candidate.ACTIVE}


@router.delete("/candidates/{candidate_id}", status_code=204)
async def delete_candidate(request: Request, candidate_id: uuid.UUID) -> Response:
    """Erase a person. Owner only, and irreversible.

    Skills and overrides cascade. Nothing else in phase 1 holds this person's
    personal data — the bulk import that will is built in phase 2, and its plan
    must extend this endpoint to scrub `candidate_import_rows`.
    """
    user_uuid, tenant_uuid = _require_session(request)
    await _require_owner(user_uuid, tenant_uuid)
    async with tenant_session(tenant_uuid) as session:
        await _load(session, candidate_id)
        await session.execute(delete(Candidate).where(Candidate.id == candidate_id))
        await session.commit()
    return Response(status_code=204)


@router.get("/candidates/{candidate_id}/export")
async def export_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    """Everything stored about one person, for a data-access request."""
    return await get_candidate(request, candidate_id)


async def _require_owner(user_uuid: uuid.UUID, tenant_uuid: uuid.UUID) -> None:
    """The first role check in this codebase — see the spec.

    Archiving is what recruiters do daily and is open to everyone. Deleting is
    irreversible and covers personal data, so it is the owner's to do.
    """
    async with tenant_session(tenant_uuid) as session:
        role = (
            await session.execute(
                select(User.role).where(User.id == user_uuid)
            )
        ).scalar_one_or_none()
    if role != "owner":
        raise HTTPException(
            status_code=403, detail="Only the account owner can delete a candidate"
        )


async def _replace_skills(
    session, tenant_uuid: uuid.UUID, candidate_id: uuid.UUID, skills: list[str]
) -> None:
    """Skills are a set: the payload replaces them rather than appending.

    An append-only list has no way to remove a skill somebody typed by
    mistake, and a form that cannot unsay something is a form people distrust.
    """
    await session.execute(
        delete(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id)
    )
    seen: set[str] = set()
    for raw in skills:
        normalized = normalize_skill(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        await session.execute(
            insert(CandidateSkill).values(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                candidate_id=candidate_id,
                skill=raw.strip(),
                skill_normalized=normalized,
            )
        )
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_candidates_api.py tests/test_routing.py -v && uv run ruff check .
```

Expected: all passed.

- [ ] **Step 5: Run the whole suite**

```bash
uv run pytest -q
```

Expected: no new failures. There are known pre-existing failures caused by an empty `REDIS_URL` in `app/workers/queue.py`; confirm the count and names match what was failing before your change rather than assuming.

- [ ] **Step 6: Commit**

```bash
git add app/api/candidates.py tests/test_candidates_api.py
git commit -m "Let a person be created, corrected, merged and erased"
```

---

### Task 8: The candidates screen

**Files:**
- Create: `frontend/app/dashboard/candidates.ts`
- Create: `frontend/app/dashboard/candidates/page.tsx`
- Create: `frontend/app/dashboard/candidates/candidates-table.tsx`
- Create: `frontend/app/dashboard/candidates/candidate-panel.tsx`
- Create: `frontend/app/dashboard/candidates/candidate-form.tsx`

**Interfaces:**
- Consumes: the endpoints from Tasks 6 and 7.
- Produces: a `/dashboard/candidates` route.

**There is no frontend test framework** — `package.json` has only `dev`, `build`, `start`, `lint`. Verification is `npm run lint`, `npm run build` (which type-checks), and the stated manual checks. Do not add a test framework here; that is its own decision, not a side effect of this feature.

`job-orders-table.tsx` and `detail-panel.tsx` are **opportunity-specific**, not generic — they hardcode their columns and types. Write candidate equivalents beside them rather than refactoring them into shared components; a generalisation driven by a second use case usually guesses the wrong seams.

- [ ] **Step 1: Write the data module**

Create `frontend/app/dashboard/candidates.ts`, following the fetch and typing pattern in `frontend/app/dashboard/opportunities.ts` exactly — read that file first. It must use `credentials: "include"` and `Accept: application/json`, as that file does, and build its query string with `URLSearchParams`.

Export:

```typescript
export type Candidate = {
  id: string;
  full_name: string;
  email: string | null;
  phone_raw: string | null;
  current_title: string | null;
  current_employer: string | null;
  location: string | null;
  years_experience: number | null;
  expected_salary: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  available_from: string | null;
  notice_period_raw: string | null;
  employment_type: string | null;
  notes: string | null;
  pipeline_stage: Stage;
  record_status: "active" | "archived" | "merged";
  skills?: string[];
  overridden_fields?: string[];
};

export type Stage = "new" | "contacted" | "submitted" | "placed" | "rejected";

export type CandidatePage = {
  items: Candidate[];
  total: number;
  limit: number;
  offset: number;
  counts: Record<string, number>;
};

export async function listCandidates(
  opts: { stage?: Stage; q?: string; offset?: number },
): Promise<CandidatePage>;
export async function getCandidate(id: string): Promise<Candidate>;
export async function createCandidate(body: Partial<Candidate> & { full_name: string }): Promise<Candidate>;
export async function updateCandidate(id: string, body: Partial<Candidate>): Promise<Candidate>;
export async function archiveCandidate(id: string): Promise<void>;
export async function deleteCandidate(id: string): Promise<void>;
```

`createCandidate` and `updateCandidate` must surface a 409 as a readable error rather than throwing a generic failure — a split-identity conflict returns a message naming both candidates, and that message is the only thing that tells the recruiter what to do next.

- [ ] **Step 2: Build the page**

Create `frontend/app/dashboard/candidates/page.tsx` as a `"use client"` component, composed the way `frontend/app/dashboard/page.tsx` composes its dashboard — reuse `useAuth()` and the same anonymous-user redirect to `LANDING_PATH`, rather than inventing a second auth pattern.

It holds: stage chips driven by `counts`, a search box bound to `q`, the table, the detail panel, and an "Add candidate" button opening the form.

- [ ] **Step 3: Build the table, panel and form**

`candidates-table.tsx` — columns: name, current title, employer, stage, updated. Props `{ rows: Candidate[]; selectedId: string | null; onSelect: (id: string) => void }`.

`candidate-panel.tsx` — full record plus skills. Props `{ row: Candidate | null; onEdit: () => void; onArchive: () => Promise<void>; onDelete: (() => Promise<void>) | null }`. `onDelete` is `null` for a recruiter, so the button is absent rather than present-and-failing — a 403 the user could have been spared is a worse experience than no button.

A field listed in `overridden_fields` renders with a marker and the title text "Edited by hand — an import will not change this." That is the whole reason the overrides table exists, and it is invisible without this.

`candidate-form.tsx` — create and edit. Only `full_name` is required; the submit button stays disabled while it is blank. On a 409, the server's message renders inline above the form.

- [ ] **Step 4: Verify**

```bash
cd frontend && npm run lint && npm run build
```

Expected: both succeed. `npm run build` type-checks, so a mismatch between `Candidate` here and the API's payload surfaces as a build error.

- [ ] **Step 5: Check it by hand**

Start the backend and the frontend, sign in, and confirm each of these. The suite cannot check any of them, so they are the only evidence this screen works:

1. The list loads and the stage chips show tenant-wide counts that do not change as you page.
2. Adding a candidate with only a name succeeds.
3. Adding a second candidate with an email that already exists shows the conflict message, not a blank failure.
4. Editing a title, then reopening the record, shows the "edited by hand" marker on that field.
5. Signed in as a recruiter (set `role` to `recruiter` in the database), the delete button is absent; as an owner it is present and works.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/dashboard/candidates.ts frontend/app/dashboard/candidates
git commit -m "Give recruiters a screen for the people they place"
```

---

## What this plan does not build

Recorded so absence is not mistaken for oversight:

- **CSV/XLSX import** — phase 2, its own plan. This plan builds the two things it depends on: the matcher and the overrides table.
- **The retention purge worker.** `retention_until` is still written and never read, platform-wide. Candidate deletion is manual until that exists.
- **A frontend test framework.** None exists; adding one is a decision in its own right, not a side effect of this feature.
- **Role checks anywhere else.** This plan adds the first one, on delete only. Every other endpoint in the codebase remains open to any signed-in user of the tenant.
