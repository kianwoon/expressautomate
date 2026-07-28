# Candidate Work History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each candidate a list of the roles they actually held, typed by a recruiter, and derive the candidate's current title, employer and years of experience from it.

**Architecture:** One new tenant-scoped table, `candidate_roles`, with a composite `(tenant_id, candidate_id)` foreign key. A pure-function service computes tenure from role spans; the API layer calls it inside the same transaction as any role mutation and writes the result back to the cached scalar columns on `candidates`. The UI renders a timeline inside the candidate detail panel.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0 (`Mapped` / `mapped_column`), Alembic, Postgres 16 with row-level security, pytest, Next.js static export with plain CSS.

## Global Constraints

- All config comes from the repo-root `.env` via `app.core.config.settings`. **No hardcoded URLs, model names, keys, limits or TTLs.**
- Every business table carries `tenant_id` via the `TenantScoped` mixin (`app/db/base.py:33`).
- **No source file may exceed 1500 lines.** `backend/app/api/candidates.py` is 823. `frontend/app/globals.css` is 1496 — Task 5 exists because of this.
- Every API route lives under `/api`; `backend/tests/test_routing.py` fails if one escapes.
- A candidate belonging to another tenant is **404, never 403**.
- Tests never touch the live database. `backend/tests/conftest.py` refuses a non-local host.
- The AI must not fabricate missing values (§15). No date component may be invented — hence the precision columns.

**Running the backend test suite in this worktree.** The worktree `.env` symlink points at the production database, which `conftest.py` correctly refuses. Use the CI-style local roles against the `ea-test-db` container:

```bash
DATABASE_ADMIN_URL=postgresql://postgres:postgres@localhost:5433/expressautomate DATABASE_URL=postgresql://expressautomate_app:ci-app-password@localhost:5433/expressautomate uv run pytest -q
```

Baseline before this plan: **821 passed**.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/models/candidate.py` (modify) | Add `CandidateRole`. Candidate-owned tables already live here (`CandidateSkill`, `CandidateFieldOverride`); a role belongs with them. |
| `backend/alembic/versions/20260728_1800_candidate_roles.py` (create) | Table, constraints, RLS policy. |
| `backend/app/services/candidate_tenure.py` (create) | Pure functions: span arithmetic, union of months, derived profile. No database, no I/O — the part most likely to be wrong, made trivially testable. |
| `backend/app/api/candidate_roles.py` (create) | The three routes plus the write-back. Separate file: `candidates.py` is 823 lines. |
| `backend/app/api/candidates.py` (modify) | Embed roles in the existing candidate GET. |
| `backend/app/main.py` (modify) | Register the router. |
| `backend/tests/test_candidate_tenure.py` (create) | Derivation arithmetic. |
| `backend/tests/test_candidate_roles_api.py` (create) | Routes, isolation, validation. |
| `frontend/app/app.css` (create) | App-only class families moved out of `globals.css`. |
| `frontend/app/layout.tsx` (modify) | Import the second sheet. |
| `frontend/app/globals.css` (modify) | Shrink by the moved families. |
| `frontend/app/dashboard/candidates.ts` (modify) | `CandidateRole` type and the three fetch functions. |
| `frontend/app/api.ts` (modify) | Path helpers. |
| `frontend/app/dashboard/candidates/candidate-history.tsx` (create) | Timeline plus inline editor. |
| `frontend/app/dashboard/candidates/candidate-panel.tsx` (modify) | Mount the section. |

---

### Task 1: The `candidate_roles` table

**Files:**
- Modify: `backend/app/models/candidate.py` (append after `CandidateSkill`, which ends at line 163)
- Create: `backend/alembic/versions/20260728_1800_candidate_roles.py`
- Test: `backend/tests/test_candidate_roles_api.py`

**Interfaces:**
- Consumes: `Base`, `UUIDPrimaryKey`, `TenantScoped`, `Timestamps` from `app.db.base`.
- Produces: `CandidateRole` with columns `candidate_id`, `employer`, `employer_normalized`, `title`, `title_normalized`, `started_on`, `started_precision`, `ended_on`, `ended_precision`, `employment_type`, `location`, `description`, `source`, `status`, `extraction_id`, `created_by`, `updated_by`. Class constants `SOURCES`, `STATUSES`, `PRECISIONS`.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_candidate_roles_api.py`:

```python
"""Roles a candidate held. Typed by a person; nothing here is AI-derived yet."""

import pytest
from sqlalchemy import select

from app.models.candidate import CandidateRole


@pytest.mark.asyncio
async def test_a_role_belongs_to_one_tenant_only(tenant_session_factory, candidate_factory):
    """Agency B cannot see Agency A's role even knowing its id."""
    a_candidate, a_tenant = await candidate_factory()
    _b_candidate, b_tenant = await candidate_factory()

    async with tenant_session_factory(a_tenant) as session:
        session.add(
            CandidateRole(
                tenant_id=a_tenant,
                candidate_id=a_candidate,
                employer="Parkway Shenton",
                employer_normalized="parkway shenton",
                title="Staff Nurse",
                title_normalized="staff nurse",
                started_on=None,
                started_precision="month",
                source=CandidateRole.HUMAN,
                status=CandidateRole.CONFIRMED,
            )
        )
        await session.commit()

    async with tenant_session_factory(b_tenant) as session:
        rows = (await session.execute(select(CandidateRole))).scalars().all()
        assert rows == []
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd backend && DATABASE_ADMIN_URL=postgresql://postgres:postgres@localhost:5433/expressautomate DATABASE_URL=postgresql://expressautomate_app:ci-app-password@localhost:5433/expressautomate uv run pytest tests/test_candidate_roles_api.py -q
```

Expected: `ImportError: cannot import name 'CandidateRole'`.

If `tenant_session_factory` or `candidate_factory` do not exist in `backend/tests/conftest.py`, read that file and use whatever fixtures the neighbouring `tests/test_candidate_isolation.py` uses instead — match the existing suite, do not invent fixtures.

- [ ] **Step 3: Add the model**

Append to `backend/app/models/candidate.py`:

```python
class CandidateRole(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One job a candidate held.

    The level beneath the flat candidate row: `current_title` says where
    somebody is, this says how they got there, which is the part sourcing can
    reason about.

    Dates carry their own precision because a CV that says "Mar 2019" does not
    say the day. Storing `2019-03-01` and rendering "1 March 2019" would assert
    a fact no source ever stated (§15), so the precision travels with the date
    and the UI renders only what was actually known.

    `source` and `status` have exactly one value each today — a recruiter types
    these rows and they are confirmed on arrival. They exist now because the CV
    parser and the importers land in this same table later, and adding the
    columns then means a migration across every tenant's live data.
    """

    __tablename__ = "candidate_roles"

    HUMAN = "human"
    SOURCES = (HUMAN, "cv_upload", "email_attachment", "import")

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    STATUSES = (UNCONFIRMED, CONFIRMED, REJECTED)

    PRECISIONS = ("year", "month", "day")

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )

    # Raw beside normalised, the same rule `opportunities` and `candidates`
    # follow: the recruiter recognises what they typed, and only the normalised
    # form can be compared against a job order's company name.
    employer: Mapped[str] = mapped_column(Text, nullable=False)
    employer_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # Nullable because a CV that gives no dates at all is still worth recording
    # — the employer and title alone are a matchable fact. A NULL `ended_on`
    # with a non-NULL `started_on` means the role is current.
    started_on: Mapped[date | None] = mapped_column(Date)
    started_precision: Mapped[str | None] = mapped_column(String(8))
    ended_on: Mapped[date | None] = mapped_column(Date)
    ended_precision: Mapped[str | None] = mapped_column(String(8))

    employment_type: Mapped[str | None] = mapped_column(String(32))
    location: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    source: Mapped[str] = mapped_column(String(24), nullable=False, default=HUMAN)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CONFIRMED, index=True
    )
    # Set only on a row a model produced, so the evidence behind it can be
    # found. Always NULL while a person is the only writer.
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_roles_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        # No unique constraint on (candidate, employer, title, started_on).
        # Somebody can genuinely hold the same title at the same employer
        # twice, having left and returned, and refusing the second one would
        # be the system telling a recruiter their own record is wrong.
        CheckConstraint(
            "ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on",
            name="ck_candidate_roles_ends_after_start",
        ),
        Index("ix_candidate_roles_candidate_started", "candidate_id", "started_on"),
    )
```

Add `CheckConstraint` to the `sqlalchemy` import block at line 21 and `date` is already imported at line 19.

- [ ] **Step 4: Write the migration**

Create `backend/alembic/versions/20260728_1800_candidate_roles.py`. Read `backend/alembic/versions/20260728_1700_candidate_avatar.py` for the header format and set `down_revision` to that file's `revision` value. RLS follows `20260726_1800_row_level_security.py:93-102` exactly:

```python
"""candidate roles

The RLS policy is created in the same revision as the table on purpose.
`verify_rls_enforced()` (`app/db/rls.py:58`) refuses to boot on any readable
table without a forced policy, so a policy added in a later revision is a
failed deploy rather than a silent cross-tenant leak.
"""

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision = "b7c1e4a2d905"
down_revision = "1519048c9751"
branch_labels = None
depends_on = None

_SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "candidate_roles",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employer", sa.Text(), nullable=False),
        sa.Column("employer_normalized", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_normalized", sa.Text(), nullable=False),
        sa.Column("started_on", sa.Date()),
        sa.Column("started_precision", sa.String(8)),
        sa.Column("ended_on", sa.Date()),
        sa.Column("ended_precision", sa.String(8)),
        sa.Column("employment_type", sa.String(32)),
        sa.Column("location", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("source", sa.String(24), nullable=False, server_default="human"),
        sa.Column("status", sa.String(16), nullable=False, server_default="confirmed"),
        sa.Column("extraction_id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("updated_by", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_roles_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "ended_on IS NULL OR started_on IS NULL OR ended_on >= started_on",
            name="ck_candidate_roles_ends_after_start",
        ),
    )
    op.create_index("ix_candidate_roles_candidate_id", "candidate_roles", ["candidate_id"])
    op.create_index("ix_candidate_roles_status", "candidate_roles", ["status"])
    op.create_index("ix_candidate_roles_employer_normalized", "candidate_roles", ["employer_normalized"])
    op.create_index("ix_candidate_roles_title_normalized", "candidate_roles", ["title_normalized"])
    op.create_index(
        "ix_candidate_roles_candidate_started", "candidate_roles", ["candidate_id", "started_on"]
    )

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON candidate_roles TO "{settings.DATABASE_APP_ROLE}"')

    predicate = f"tenant_id = nullif(current_setting('{_SETTING}', true), '')::uuid"
    op.execute("ALTER TABLE candidate_roles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE candidate_roles FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_roles")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON candidate_roles
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_roles")
    op.drop_table("candidate_roles")
```

- [ ] **Step 5: Apply the migration locally and run the test**

```bash
cd backend && DATABASE_ADMIN_URL=postgresql://postgres:postgres@localhost:5433/expressautomate DATABASE_URL=postgresql://expressautomate_app:ci-app-password@localhost:5433/expressautomate uv run pytest tests/test_candidate_roles_api.py -q
```

Expected: PASS. Do **not** run `alembic upgrade` against the production database — deploying the branch does that.

- [ ] **Step 6: Prove there is no autogenerate drift**

```bash
cd backend && DATABASE_ADMIN_URL=postgresql://postgres:postgres@localhost:5433/expressautomate DATABASE_URL=postgresql://expressautomate_app:ci-app-password@localhost:5433/expressautomate uv run alembic check
```

Expected: `No new upgrade operations detected.` If it proposes changes, the model and the migration disagree — fix the migration, not the model.

- [ ] **Step 7: Full suite and commit**

```bash
cd backend && uv run ruff check .
```

Expected: `All checks passed!` Then the full suite (expect 821 + your new tests), then:

```bash
git add backend/app/models/candidate.py backend/alembic/versions/20260728_1800_candidate_roles.py backend/tests/test_candidate_roles_api.py
git commit -m "Give a candidate the roles they held, not just the one they hold"
```

---

### Task 2: Tenure arithmetic

**Files:**
- Create: `backend/app/services/candidate_tenure.py`
- Test: `backend/tests/test_candidate_tenure.py`

**Interfaces:**
- Consumes: nothing. Pure functions, no database, no settings.
- Produces:
  - `span_months(started_on: date, started_precision: str | None, ended_on: date | None, ended_precision: str | None, today: date) -> tuple[date, date]` — the resolved half-open interval.
  - `union_months(spans: list[tuple[date, date]]) -> int` — total months covered by the union.
  - `DerivedProfile` dataclass with `current_title: str | None`, `current_employer: str | None`, `years_experience: int | None`, `is_current: bool`.
  - `derive(roles: list, today: date) -> DerivedProfile` — `roles` is any sequence of objects with the `CandidateRole` attribute names.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_candidate_tenure.py`:

```python
"""The arithmetic behind years_experience.

Separated from the API because this is the part most likely to be wrong and
the part cheapest to test: no database, no request, no tenant.
"""

from datetime import date

from app.models.candidate import CandidateRole
from app.services.candidate_tenure import derive, union_months


class _Role:
    """Stands in for a CandidateRole without touching the database."""

    def __init__(self, employer, title, started_on, ended_on=None, started_precision="month", ended_precision="month"):
        self.employer = employer
        self.title = title
        self.started_on = started_on
        self.ended_on = ended_on
        self.started_precision = started_precision
        self.ended_precision = ended_precision
        self.status = CandidateRole.CONFIRMED


def test_two_concurrent_roles_count_once():
    """The union, not the sum. Two jobs through 2020 is one year, not two."""
    spans = [(date(2020, 1, 1), date(2021, 1, 1)), (date(2020, 1, 1), date(2021, 1, 1))]
    assert union_months(spans) == 12


def test_partly_overlapping_roles_count_the_covered_months():
    spans = [(date(2020, 1, 1), date(2020, 7, 1)), (date(2020, 4, 1), date(2020, 10, 1))]
    assert union_months(spans) == 9


def test_a_gap_between_roles_is_not_counted():
    spans = [(date(2018, 1, 1), date(2019, 1, 1)), (date(2021, 1, 1), date(2022, 1, 1))]
    assert union_months(spans) == 24


def test_an_open_ended_role_counts_up_to_today():
    roles = [_Role("Parkway Shenton", "Staff Nurse", date(2023, 1, 1), None)]
    assert derive(roles, today=date(2026, 1, 1)).years_experience == 3


def test_year_precision_counts_from_mid_year():
    """"2019 to 2021" is somewhere between one and three years.

    July avoids a bias in either direction; January would systematically
    overstate and December understate.
    """
    roles = [
        _Role("Coda", "Engineer", date(2019, 1, 1), date(2021, 1, 1), "year", "year")
    ]
    assert derive(roles, today=date(2026, 1, 1)).years_experience == 2


def test_the_open_ended_role_is_the_current_one():
    roles = [
        _Role("Old Place", "Junior", date(2015, 1, 1), date(2019, 1, 1)),
        _Role("Parkway Shenton", "Staff Nurse", date(2019, 2, 1), None),
    ]
    profile = derive(roles, today=date(2026, 1, 1))
    assert profile.current_employer == "Parkway Shenton"
    assert profile.is_current is True


def test_between_jobs_names_the_most_recent_role_and_says_it_is_not_current():
    roles = [_Role("Old Place", "Junior", date(2015, 1, 1), date(2019, 1, 1))]
    profile = derive(roles, today=date(2026, 1, 1))
    assert profile.current_employer == "Old Place"
    assert profile.is_current is False


def test_a_role_with_no_dates_still_names_the_employer():
    roles = [_Role("Coda", "Engineer", None, None)]
    profile = derive(roles, today=date(2026, 1, 1))
    assert profile.current_employer == "Coda"
    assert profile.years_experience is None


def test_no_roles_derives_nothing():
    profile = derive([], today=date(2026, 1, 1))
    assert profile.current_employer is None
    assert profile.years_experience is None
```

- [ ] **Step 2: Run and watch them fail**

```bash
cd backend && DATABASE_ADMIN_URL=postgresql://postgres:postgres@localhost:5433/expressautomate DATABASE_URL=postgresql://expressautomate_app:ci-app-password@localhost:5433/expressautomate uv run pytest tests/test_candidate_tenure.py -q
```

Expected: `ModuleNotFoundError: No module named 'app.services.candidate_tenure'`.

- [ ] **Step 3: Implement**

Create `backend/app/services/candidate_tenure.py`:

```python
"""How long somebody has worked, computed from the roles they held.

Kept apart from the API because it is arithmetic, and arithmetic is worth
testing without a database in the way. Every function here is pure.
"""

from dataclasses import dataclass
from datetime import date

# A year-only date says nothing about the month. Counting from January would
# overstate every such role by up to a year and December would understate it;
# July splits the difference, so the error is bounded and unbiased.
_YEAR_ONLY_MONTH = 7


@dataclass(frozen=True)
class DerivedProfile:
    current_title: str | None
    current_employer: str | None
    years_experience: int | None
    is_current: bool


def _resolve(day: date, precision: str | None) -> date:
    """Pin a stored date to the point its precision actually supports."""
    if precision == "year":
        return date(day.year, _YEAR_ONLY_MONTH, 1)
    return date(day.year, day.month, 1)


def span_months(
    started_on: date,
    started_precision: str | None,
    ended_on: date | None,
    ended_precision: str | None,
    today: date,
) -> tuple[date, date]:
    """The half-open interval a role covers, both ends pinned to a month."""
    start = _resolve(started_on, started_precision)
    end = _resolve(ended_on, ended_precision) if ended_on else date(today.year, today.month, 1)
    if end < start:
        end = start
    return (start, end)


def union_months(spans: list[tuple[date, date]]) -> int:
    """Months covered by at least one span.

    The union rather than the sum: somebody who held two jobs through 2020
    gained one year of experience, not two, and a sum would quietly inflate
    every candidate who ever moonlighted.
    """
    if not spans:
        return 0
    months = 0
    cursor: date | None = None
    for start, end in sorted(spans):
        begin = start if cursor is None or start > cursor else cursor
        if end > begin:
            months += (end.year - begin.year) * 12 + (end.month - begin.month)
            cursor = end
        elif cursor is None or end > cursor:
            cursor = end
    return months


def derive(roles: list, today: date) -> DerivedProfile:
    """What the candidate row should say, given these roles."""
    live = [r for r in roles if getattr(r, "status", None) != "rejected"]
    if not live:
        return DerivedProfile(None, None, None, False)

    spans = [
        span_months(r.started_on, r.started_precision, r.ended_on, r.ended_precision, today)
        for r in live
        if r.started_on is not None
    ]
    months = union_months(spans) if spans else None

    # Current means open-ended and latest-started. Failing that, the role that
    # ended most recently — a candidate between jobs still has a last employer,
    # and the panel labels it "Most recently" rather than claiming otherwise.
    open_ended = [r for r in live if r.started_on is not None and r.ended_on is None]
    if open_ended:
        latest = max(open_ended, key=lambda r: r.started_on)
        is_current = True
    else:
        ended = [r for r in live if r.ended_on is not None]
        if ended:
            latest = max(ended, key=lambda r: r.ended_on)
        else:
            latest = live[0]
        is_current = False

    return DerivedProfile(
        current_title=latest.title,
        current_employer=latest.employer,
        years_experience=months // 12 if months is not None else None,
        is_current=is_current,
    )
```

- [ ] **Step 4: Run and watch them pass**

```bash
cd backend && DATABASE_ADMIN_URL=postgresql://postgres:postgres@localhost:5433/expressautomate DATABASE_URL=postgresql://expressautomate_app:ci-app-password@localhost:5433/expressautomate uv run pytest tests/test_candidate_tenure.py -q
```

Expected: `9 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/candidate_tenure.py backend/tests/test_candidate_tenure.py
git commit -m "Count experience as the months worked, not the months summed"
```

---

### Task 3: The routes, and the write-back

**Files:**
- Create: `backend/app/api/candidate_roles.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/candidates.py` (`_serialize`, around line 40)
- Test: `backend/tests/test_candidate_roles_api.py`

**Interfaces:**
- Consumes: `derive`, `DerivedProfile` from Task 2; `_load` from `app.api.candidates:209`; `tenant_session` from `app.db.rls`; `_require_session` from `app.api.auth`.
- Produces: `apply_derived(session, candidate) -> None`, and role dicts shaped `{id, employer, title, started_on, started_precision, ended_on, ended_precision, employment_type, location, description, source, status, is_current}`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_candidate_roles_api.py`. Match the client style used in `tests/test_candidates_api.py` — read it first and reuse its auth fixture rather than inventing one.

```python
@pytest.mark.asyncio
async def test_adding_a_role_updates_the_candidate_row(client, signed_in):
    candidate = await _a_candidate(client, full_name="Tan Hui Ling")

    res = await client.post(
        f"/api/candidates/{candidate['id']}/roles",
        json={
            "employer": "Raffles Medical",
            "title": "Enrolled Nurse",
            "started_on": "2019-03-01",
            "started_precision": "month",
        },
    )
    assert res.status_code == 201

    again = await client.get(f"/api/candidates/{candidate['id']}")
    body = again.json()
    assert body["current_employer"] == "Raffles Medical"
    assert body["current_title"] == "Enrolled Nurse"
    assert len(body["roles"]) == 1


@pytest.mark.asyncio
async def test_a_role_that_ends_before_it_starts_is_a_422(client, signed_in):
    candidate = await _a_candidate(client, full_name="Tan Hui Ling")
    res = await client.post(
        f"/api/candidates/{candidate['id']}/roles",
        json={
            "employer": "Raffles Medical",
            "title": "Enrolled Nurse",
            "started_on": "2020-01-01",
            "ended_on": "2019-01-01",
        },
    )
    assert res.status_code == 422
    assert "end" in res.json()["detail"][0]["msg"].lower()


@pytest.mark.asyncio
async def test_another_agencys_candidate_is_a_404_not_a_403(client, signed_in, other_tenant_candidate_id):
    res = await client.post(
        f"/api/candidates/{other_tenant_candidate_id}/roles",
        json={"employer": "Coda", "title": "Engineer"},
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_a_years_experience_override_survives_derivation(client, signed_in):
    """A person asserted this. Adding a role must not quietly overwrite it."""
    candidate = await _a_candidate(client, full_name="Tan Hui Ling")
    await client.patch(
        f"/api/candidates/{candidate['id']}", json={"years_experience": 20}
    )
    await client.post(
        f"/api/candidates/{candidate['id']}/roles",
        json={
            "employer": "Raffles Medical",
            "title": "Enrolled Nurse",
            "started_on": "2024-01-01",
            "started_precision": "month",
        },
    )
    body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["years_experience"] == 20


@pytest.mark.asyncio
async def test_deleting_the_last_role_leaves_the_cached_columns_alone(client, signed_in):
    candidate = await _a_candidate(client, full_name="Tan Hui Ling")
    created = (
        await client.post(
            f"/api/candidates/{candidate['id']}/roles",
            json={
                "employer": "Raffles Medical",
                "title": "Enrolled Nurse",
                "started_on": "2019-03-01",
                "started_precision": "month",
            },
        )
    ).json()

    await client.delete(f"/api/candidates/{candidate['id']}/roles/{created['id']}")

    body = (await client.get(f"/api/candidates/{candidate['id']}")).json()
    assert body["roles"] == []
    assert body["current_employer"] == "Raffles Medical"


@pytest.mark.asyncio
async def test_roles_come_back_current_first_then_newest(client, signed_in):
    candidate = await _a_candidate(client, full_name="Tan Hui Ling")
    for employer, started, ended in [
        ("Oldest", "2010-01-01", "2013-01-01"),
        ("Middle", "2015-01-01", "2018-01-01"),
        ("Current", "2019-01-01", None),
    ]:
        payload = {
            "employer": employer,
            "title": "Nurse",
            "started_on": started,
            "started_precision": "month",
        }
        if ended:
            payload["ended_on"] = ended
            payload["ended_precision"] = "month"
        await client.post(f"/api/candidates/{candidate['id']}/roles", json=payload)

    roles = (await client.get(f"/api/candidates/{candidate['id']}")).json()["roles"]
    assert [r["employer"] for r in roles] == ["Current", "Middle", "Oldest"]
```

Write the `_a_candidate` helper by copying the candidate-creation call already used in `tests/test_candidates_api.py`.

- [ ] **Step 2: Run and watch them fail**

Expected: 404s on every `/roles` route, because none is registered yet.

- [ ] **Step 3: Implement the routes**

Create `backend/app/api/candidate_roles.py`. Key requirements, all of which the tests above check:

```python
"""The roles a candidate held.

A separate module from `candidates.py`, which is already 823 lines against a
1500-line ceiling. The routes nest under the candidate because a role has no
meaning apart from one.
"""

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from app.api.auth import _require_session
from app.api.candidates import _load
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateFieldOverride, CandidateRole
from app.services.candidate_naming import normalize_company_name
from app.services.candidate_tenure import derive

router = APIRouter(tags=["candidate-roles"])


class _RoleBody(BaseModel):
    employer: str
    title: str
    started_on: date | None = None
    started_precision: str | None = None
    ended_on: date | None = None
    ended_precision: str | None = None
    employment_type: str | None = None
    location: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _ends_after_it_starts(self):
        # The CHECK constraint says the same thing in the database. Saying it
        # here means the recruiter is told which end is wrong, rather than
        # meeting a 500 from a violated constraint.
        if self.started_on and self.ended_on and self.ended_on < self.started_on:
            raise ValueError("A role cannot end before it starts")
        for value in (self.started_precision, self.ended_precision):
            if value is not None and value not in CandidateRole.PRECISIONS:
                raise ValueError(f"precision must be one of {CandidateRole.PRECISIONS}")
        return self


async def _roles_for(session, candidate_id: uuid.UUID) -> list[CandidateRole]:
    return list(
        (
            await session.execute(
                select(CandidateRole).where(CandidateRole.candidate_id == candidate_id)
            )
        ).scalars()
    )


async def apply_derived(session, candidate: Candidate) -> None:
    """Write the derived profile back onto the candidate row.

    Called inside the same transaction as the mutation that changed the roles,
    so a failed recompute takes the role change with it. A role saved beside a
    candidate row that still disagrees with it is exactly the drift this
    design exists to prevent.
    """
    roles = await _roles_for(session, candidate.id)
    if not roles:
        # Deliberately not clearing the columns. Emptying a recruiter's screen
        # because they tidied one history entry is worse than slight staleness.
        return

    profile = derive(roles, today=date.today())

    overridden = set(
        (
            await session.execute(
                select(CandidateFieldOverride.field_name).where(
                    CandidateFieldOverride.candidate_id == candidate.id
                )
            )
        )
        .scalars()
    )

    if "current_title" not in overridden and profile.current_title:
        candidate.current_title = profile.current_title
    if "current_employer" not in overridden and profile.current_employer:
        candidate.current_employer = profile.current_employer
    if "years_experience" not in overridden and profile.years_experience is not None:
        candidate.years_experience = profile.years_experience
```

Then the three routes. Each one: `_require_session`, `async with tenant_session(tenant_uuid)`, `await _load(session, candidate_id)` (which 404s for another tenant), mutate, `await apply_derived(session, candidate)`, `await session.commit()`.

`POST` returns 201 with the created role. `PATCH` loads the role scoped by both `candidate_id` and its own id, 404 if absent. `DELETE` returns 204.

Set `employer_normalized` and `title_normalized` with `normalize_company_name` — check that function's real name in `app/services/candidate_naming.py` and use whatever normaliser already exists rather than writing a new one.

- [ ] **Step 4: Register the router**

In `backend/app/main.py`, beside the other `include_router` calls:

```python
from app.api import candidate_roles

app.include_router(candidate_roles.router, prefix="/api")
```

Match the exact prefix idiom the neighbouring routers use.

- [ ] **Step 5: Embed roles in the candidate GET**

In `backend/app/api/candidates.py`, the single-record GET adds `"roles"` to its response, ordered current-first then newest by `started_on` with NULLs last. The list endpoint does **not** — a table of fifty candidates does not need everybody's career.

- [ ] **Step 6: Run the tests, then the whole suite**

Expected: the six new tests pass, and the suite is at its previous count plus the new ones with **no** pre-existing test failing.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/candidate_roles.py backend/app/main.py backend/app/api/candidates.py backend/tests/test_candidate_roles_api.py
git commit -m "Let a recruiter record a career, and keep the summary honest"
```

---

### Task 4: Split the stylesheet

**Files:**
- Create: `frontend/app/app.css`
- Modify: `frontend/app/globals.css`
- Modify: `frontend/app/layout.tsx:2`

**Interfaces:**
- Consumes: nothing.
- Produces: a `globals.css` with room to grow, and `app.css` holding the app-only class families.

This task exists solely because `globals.css` is at 1496 lines against a hard 1500-line ceiling, and Task 5 cannot add a rule until it is under.

**Move by class family, not by line range.** An earlier attempt to name line numbers for this cut was wrong — the file's section boundaries do not fall where a summary claimed. Move these families, verifying each with a search before and after:

- `.jo-*` — the dashboard workspace, job-order table, sync activity, decoded shorthand
- `.ca-*` — the candidate avatar
- `.gl-*` — the glossary
- `.nt-*` — notifications

**Leave shared families in `globals.css`:** `:root`, resets, `.body`, `.eyebrow`, `.btn*`, `.rows`, `.muted`, nav, footer, and every `@media` block — the responsive rules override both landing and app selectors and splitting them invites a subtle regression.

**Import both sheets in `layout.tsx`,** not in a dashboard-only layout. `settings/` uses `jo-*` classes too, so a dashboard-scoped import would silently unstyle the settings screens.

```tsx
import "./globals.css";
import "./app.css";
```

- [ ] **Step 1: Record what the pages look like now**

```bash
cd frontend && npm run build
```

Expected: `✓ Compiled successfully`. Note the reported size of `/dashboard/candidates`.

- [ ] **Step 2: Move the families**

Cut each family's rules from `globals.css` into `app.css`, keeping their relative order and their comments. Give `app.css` a header:

```css
/* Styles for the signed-in application: the dashboard, the candidate and
   client panels, the glossary and notifications.
   Split out of globals.css when that file reached its 1500-line ceiling.
   Shared primitives — the palette, type, buttons, nav, footer and every
   media query — stay in globals.css, because both halves of the product use
   them and duplicating them is how they drift. */
```

- [ ] **Step 3: Verify nothing lost a rule**

```bash
cd frontend && grep -c "" app/globals.css app/app.css
```

Expected: the two counts sum to at least 1496, and `globals.css` is now comfortably under 1500. Then:

```bash
cd frontend && npm run build && npx tsc --noEmit
```

Expected: `✓ Compiled successfully`, and `/dashboard/candidates` within a kilobyte of the size noted in Step 1. A large drop means rules were lost, not moved.

- [ ] **Step 4: Commit**

```bash
git add frontend/app/globals.css frontend/app/app.css frontend/app/layout.tsx
git commit -m "Give the application its own stylesheet, before the shared one runs out"
```

---

### Task 5: The history timeline

**Files:**
- Create: `frontend/app/dashboard/candidates/candidate-history.tsx`
- Modify: `frontend/app/dashboard/candidates.ts`
- Modify: `frontend/app/api.ts`
- Modify: `frontend/app/dashboard/candidates/candidate-panel.tsx`
- Modify: `frontend/app/app.css`

**Interfaces:**
- Consumes: `Candidate` from `candidates.ts`; the API contract from Task 3.
- Produces: `CandidateRole` type; `createCandidateRole`, `updateCandidateRole`, `deleteCandidateRole`; the `<CandidateHistory row onChanged />` component.

- [ ] **Step 1: Add the type and the fetch functions**

In `frontend/app/dashboard/candidates.ts`, add `CandidateRole` mirroring the API's role shape and `roles?: CandidateRole[]` to `Candidate` — optional, because the list endpoint does not send it and every reader must treat absent as "not loaded", not as "none". Follow the file's existing fetch idiom exactly: `credentials: "include"`, `Accept: application/json`, errors through `readError` (`candidates.ts:185`).

- [ ] **Step 2: Write the component**

`candidate-history.tsx` renders the timeline described in the spec: current roles first, employer and title on one line, the date range and duration beneath, employment type and location after, description behind a disclosure. Dates render at their recorded precision — `formatRoleDate(value, precision)` returns `"2019"` for year, `"Mar 2019"` for month, `"3 Mar 2019"` for day. Never render a day the data does not have.

The inline editor is a month/year select pair, not a date input.

Unconfirmed rows get a left border and confirm/reject buttons that are present but disabled with a title explaining they arrive with CV parsing — the state ships now so Task 5 of the decomposition is a parser and an endpoint rather than a redesign.

- [ ] **Step 3: Mount it**

In `candidate-panel.tsx`, below the field rows:

```tsx
<CandidateHistory row={row} onChanged={onAvatarChanged} />
```

Reuse `onAvatarChanged` — it is already the "refetch just this candidate, do not reload the list" callback, and a role change has exactly the same blast radius. Rename it to `onDetailChanged` in the same commit, since it now serves two callers and the old name lies.

- [ ] **Step 4: Style it in `app.css`**

New `.ch-*` family. Reuse `var(--line)`, `var(--ink-500)`, `var(--surface-alt)`. Respect `prefers-reduced-motion` on the disclosure.

- [ ] **Step 5: Verify**

```bash
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

Expected: no type errors, no new lint errors, `✓ Compiled successfully`.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/dashboard/candidates/candidate-history.tsx frontend/app/dashboard/candidates.ts frontend/app/api.ts frontend/app/dashboard/candidates/candidate-panel.tsx frontend/app/app.css
git commit -m "Show the career, not just the current job"
```

---

## Self-Review

**Spec coverage.** Table → Task 1. Precision columns → Task 1. `source`/`status` shipping early → Task 1. Union arithmetic, mid-year rule, open-ended-to-today → Task 2. Current-vs-most-recent, override survival, delete-leaves-columns → Tasks 2 and 3. Routes, 404-not-403, 422 with a readable message, embedded in GET → Task 3. Migration with RLS in one revision → Task 1. Stylesheet split → Task 4. Timeline, precision rendering, inline editing, unconfirmed styling → Task 5. All eight spec tests appear: isolation (1), union (2), gaps/open-ended/mid-year (2), override (3), delete (3), 422 (3), ordering (3), RLS (1).

**Placeholders.** None. Two steps deliberately instruct the implementer to read a neighbouring file rather than quoting it — the test fixtures in `conftest.py` and the normaliser name in `candidate_naming.py` — because quoting a signature that may have drifted is worse than pointing at the source of truth.

**Type consistency.** `derive()` and `union_months()` keep the same names and signatures across Tasks 2 and 3. `apply_derived(session, candidate)` is defined in Task 3 and used only there. `CandidateRole` class constants (`HUMAN`, `CONFIRMED`, `PRECISIONS`) are defined in Task 1 and referenced in Tasks 2 and 3. `onAvatarChanged` → `onDetailChanged` is renamed in one commit, not left inconsistent.
