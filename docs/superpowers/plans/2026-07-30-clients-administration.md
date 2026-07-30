# Clients Administration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a recruiter create, edit, suspend and administer clients by hand, alongside the ones the pipeline proposes.

**Architecture:** One Alembic migration adds a `suspended` status, nine columns to `clients`, and a `client_contacts` table with its RLS policy. `backend/app/api/clients.py` gains create, patch, suspend, unsuspend and contact CRUD alongside its existing transitions. `backend/app/api/sourcing.py` refuses a submission to a suspended client. The dashboard gains a create/edit form and suspension affordances.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, Postgres 16 with RLS, pytest/pytest-asyncio, Next.js static export, vitest.

**Spec:** [docs/superpowers/specs/2026-07-30-clients-administration-design.md](../specs/2026-07-30-clients-administration-design.md)

## Global Constraints

- **No hardcoded values.** Every configurable value comes from the repo-root `.env` via `app.core.config.settings` (`CLAUDE.md`). Free-provider domains come from `settings.FREE_EMAIL_DOMAINS`; page limits from `settings.CLIENTS_PAGE_LIMIT`.
- **Every business table carries `tenant_id`** via the `TenantScoped` mixin, and every new table gets `ENABLE` + `FORCE` row level security and a `tenant_isolation` policy in the same migration that creates it (§18).
- **Every API route lives under `/api`.** `tests/test_routing.py` fails if a route escapes it; the router in `clients.py` is already mounted under that prefix, so paths in this plan are written as the router sees them (`/clients`, not `/api/clients`).
- **No file exceeds 1500 lines.** `clients.py` is 446 lines today and will roughly double; that is fine. `client-panel.tsx` is 455 lines and must not absorb the form.
- **The AI must not fabricate missing values** (§15). An unset optional field stays NULL. No endpoint invents a default for a commercial term.
- **Removal is a status change.** No task in this plan adds a `DELETE` for a client. Contacts are the sole exception and the spec argues why.
- **Run tests with `cd backend && scripts/test-env.sh -q`** — never hand-rolled env vars and never CI's values. CI uses a different app-role password, and forcing it produces hundreds of bogus auth failures that read as flakiness. The script sources `backend/.env.test` and hides any root `.env` for the run. Lint and migrate with `uv run ruff check .` and `uv run alembic upgrade head` from `backend/`. Where a step below says `uv run pytest ...`, run `scripts/test-env.sh` with the same arguments instead.
- **Baseline before this plan:** 1208 passed, 1 skipped. Alembic head `f4b8c1e7d290`.
- **Commit after every task.** Conventional commit messages.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/alembic/versions/<rev>_client_administration.py` (create) | Schema: new `clients` columns, `client_contacts` + its RLS policy, `suspended` in any status CHECK. |
| `backend/app/models/client.py` (modify) | `Client.SUSPENDED`, new columns, `ClientContact` model. |
| `backend/app/api/integrity.py` (create) | `is_duplicate(exc)` — the SQLSTATE 23505 check, hoisted out of `candidates.py` so `clients.py` does not import from a sibling endpoint module. |
| `backend/app/api/candidates.py` (modify) | Import `is_duplicate` instead of defining it. |
| `backend/app/api/clients.py` (modify) | Create, patch, suspend, unsuspend, contact CRUD, widened `_serialize`/`StatusFilter`/`_LEGAL_SOURCES`. |
| `backend/app/api/sourcing.py` (modify) | 409 on a submission to a suspended client. |
| `backend/tests/conftest.py` (modify) | `client_contacts` cleanup statement. |
| `backend/tests/test_clients_api.py` (modify) | Every new endpoint and rule. |
| `backend/tests/test_client_matching.py` (modify) | The matcher leaves a suspended row suspended. |
| `frontend/app/dashboard/clients.ts` (modify) | Typed wrappers for the seven new calls; widened `Client` type. |
| `frontend/app/dashboard/clients/client-form.tsx` (create) | One form for create and edit, contacts inline. |
| `frontend/app/dashboard/clients/page.tsx` (modify) | Add-client button, `suspended` chip. |
| `frontend/app/dashboard/clients/clients-table.tsx` (modify) | Suspended badge. |
| `frontend/app/dashboard/clients/client-panel.tsx` (modify) | Details section, Edit button, Suspend/Unsuspend, suspension banner. |

---

## Task 1: Schema — statuses, columns, contacts table, RLS

**Files:**
- Create: `backend/alembic/versions/<rev>_client_administration.py`
- Modify: `backend/app/models/client.py`
- Modify: `backend/tests/conftest.py:96-107`
- Test: `backend/tests/test_clients_api.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Client.SUSPENDED = "suspended"`; `Client` columns `website`, `phone`, `address`, `fee_percent`, `payment_terms_days`, `notes`, `suspended_reason`, `suspended_at`, `source`; `Client.PIPELINE = "pipeline"`, `Client.MANUAL = "manual"`; `ClientContact` model with `client_id`, `name`, `email`, `phone`, `title`, `is_primary`.

- [ ] **Step 1: Check what constrains `status` today**

Run from `backend/`:

```bash
grep -rn "ck_clients_status\|status.*IN (" alembic/versions/ | head
```

If a CHECK constraint enumerates the four statuses, the migration must drop and recreate it including `suspended`. If none exists (the column is a bare `String(16)`), skip the CHECK steps below. Record which it is — the rest of this task branches on it.

- [ ] **Step 2: Add the model columns**

In `backend/app/models/client.py`, inside `class Client`, after the `ARCHIVED` constant:

```python
    SUSPENDED = "suspended"

    # How the row came to exist. Not inferable from
    # `first_seen_email_message_id`: that column is ON DELETE SET NULL, so a
    # retention purge would silently reclassify a pipeline client as manual.
    PIPELINE = "pipeline"
    MANUAL = "manual"
```

After `last_seen_at`:

```python
    # Firm-level facts a recruiter maintains by hand. All nullable: an unset
    # field is "not recorded", and nothing infers a value for it (§15).
    website: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    # A percent, because that is what a recruiter quotes: 20.00.
    fee_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    payment_terms_days: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    # A suspension is a commercial hold on a client the agency still works
    # with. Both columns are cleared by `unsuspend` and by `archive`, so a
    # stale reason can never outlive the state it describes.
    suspended_reason: Mapped[str | None] = mapped_column(Text)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[str] = mapped_column(String(16), nullable=False, default=PIPELINE)
```

Add to the imports at the top: `from decimal import Decimal` and extend the SQLAlchemy import list with `Integer` and `Numeric`.

- [ ] **Step 3: Add the `ClientContact` model**

At the end of `backend/app/models/client.py`:

```python
class ClientContact(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One person at a client company.

    Deleted outright rather than status-flagged, unlike a `ClientMention`. A
    mention is evidence that something happened, and erasing it would assert
    that it never did; a contact is a current fact about who to call, and a
    stale one is worse than an absent one.
    """

    __tablename__ = "client_contacts"

    client_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        # Composite, so a contact cannot cross agencies — the same reason
        # `client_mentions` carries one.
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_client_contacts_client_same_tenant",
            ondelete="CASCADE",
        ),
        # At most one primary per client. A partial unique INDEX cannot be
        # DEFERRABLE (only constraints can be, and Postgres has no partial
        # unique constraint), so the demote statement must run before the
        # promote statement — see `_set_primary` in app/api/clients.py.
        Index(
            "uq_client_contacts_one_primary",
            "tenant_id",
            "client_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )
```

Add `Boolean` to the SQLAlchemy import list.

- [ ] **Step 4: Generate the migration**

Run from `backend/`:

```bash
uv run alembic revision --autogenerate -m "client administration"
```

Expected: a new file under `alembic/versions/` containing `op.add_column` for each new `clients` column, `op.create_table("client_contacts", ...)`, and `op.create_index("uq_client_contacts_one_primary", ...)`.

- [ ] **Step 5: Hand-edit the migration — server default, CHECK, and RLS**

Autogenerate cannot know three things. Add them to the generated `upgrade()`.

`source` is `NOT NULL` on a table with existing rows, so it needs a server default at add-time:

```python
    op.add_column(
        "clients",
        sa.Column(
            "source", sa.String(length=16), nullable=False, server_default="pipeline"
        ),
    )
```

If Step 1 found a status CHECK, replace it (substitute the real constraint name):

```python
    op.drop_constraint("ck_clients_status", "clients", type_="check")
    op.create_check_constraint(
        "ck_clients_status",
        "clients",
        "status IN ('unconfirmed', 'confirmed', 'suspended', 'merged', 'archived')",
    )
```

And the RLS policy — copy the loop from `20260728_1100_client_profiles.py:150-166` verbatim, with a one-entry `PROTECTED`:

```python
SETTING = "app.tenant_id"
PROTECTED = (("client_contacts", "tenant_id"),)


def _enforce_rls() -> None:
    """The same policy every tenant-scoped table carries, for the same reasons.

    FORCE, not merely ENABLE: without it the table owner bypasses the policy,
    and the owner is who migrations and any superuser session connect as.

    No GRANT is needed — `ALTER DEFAULT PRIVILEGES` in
    20260726_1800_row_level_security.py already gives the runtime role DML on
    every new table. That is exactly why omitting this policy would be
    dangerous rather than merely incomplete: the role can read the table the
    moment it exists.
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
```

Call `_enforce_rls()` at the end of `upgrade()`. In `downgrade()`, drop `client_contacts` (the policy goes with the table) and drop each added column.

- [ ] **Step 6: Add the conftest cleanup line**

In `backend/tests/conftest.py`, `_CLEANUP_STATEMENTS` at line 96, add as the **first** entry (before `client_mentions`):

```python
    "DELETE FROM client_contacts WHERE tenant_id = :t",
```

The cascade covers the happy path, but each statement runs in its own transaction precisely so debris survives a partial failure — so the delete is stated rather than assumed.

- [ ] **Step 7: Write the failing schema test**

In `backend/tests/test_clients_api.py`, at the end:

```python
@pytest.mark.asyncio
async def test_client_contacts_is_tenant_isolated(agency_with_clients):
    """The new table's RLS policy, exercised through the runtime role.

    `verify_rls_enforced()` only checks the table has FORCE RLS; this checks
    the policy predicate actually filters.
    """
    from app.models.client import ClientContact

    tenant_a = agency_with_clients.tenant_id
    async with tenant_session(tenant_a) as session:
        session.add(
            ClientContact(
                tenant_id=tenant_a,
                client_id=agency_with_clients.confirmed_id,
                name="Priya",
                is_primary=True,
            )
        )
        await session.commit()

    other_tenant = uuid.uuid4()
    async with tenant_session(other_tenant) as session:
        rows = (await session.execute(select(ClientContact))).scalars().all()
    assert rows == []
```

Match the fixture's real attribute names — read `agency_with_clients` at `test_clients_api.py:24` and substitute whatever it exposes for the confirmed client's id.

- [ ] **Step 8: Run it and watch it fail**

```bash
uv run pytest tests/test_clients_api.py::test_client_contacts_is_tenant_isolated -v
```

Expected: FAIL — `relation "client_contacts" does not exist` (the test database is built from migrations).

- [ ] **Step 9: Apply the migration and re-run**

```bash
uv run alembic upgrade head && uv run pytest tests/test_clients_api.py -v && uv run ruff check .
```

Expected: PASS, including every pre-existing client test — the new columns are all nullable or defaulted, so nothing that passed before may now fail.

- [ ] **Step 10: Verify the RLS backstop agrees**

```bash
uv run pytest tests/test_rls.py -v
```

Expected: PASS. This is the check that catches a forgotten `_enforce_rls()` call.

- [ ] **Step 11: Commit**

```bash
git add backend/alembic/versions backend/app/models/client.py backend/tests/conftest.py backend/tests/test_clients_api.py
git commit -m "feat: schema for client administration and contacts"
```

---

## Task 2: Suspend and unsuspend

**Files:**
- Modify: `backend/app/api/clients.py:27` (`StatusFilter`), `:38-50` (`_serialize`), `:419-445` (`_LEGAL_SOURCES`, `_transition`)
- Test: `backend/tests/test_clients_api.py`

**Interfaces:**
- Consumes: `Client.SUSPENDED`, `Client.suspended_at`, `Client.suspended_reason` from Task 1.
- Produces: `POST /clients/{id}/suspend`, `POST /clients/{id}/unsuspend`; `_transition(request, client_id, status, extra=None)` — the fourth parameter is a `dict` of additional column values applied in the same `UPDATE`.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_clients_api.py`:

```python
@pytest.mark.asyncio
async def test_suspend_requires_confirmed(client, agency_with_clients):
    unconfirmed = agency_with_clients.unconfirmed_id
    response = await client.post(f"/api/clients/{unconfirmed}/suspend", json={})
    assert response.status_code == 400
    assert "confirm" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_suspend_then_unsuspend_returns_to_confirmed(client, agency_with_clients):
    """Unsuspend lands on `confirmed`, not `unconfirmed` — a suspension never
    revoked the judgement that the agency works with this firm."""
    target = agency_with_clients.confirmed_id

    suspended = await client.post(
        f"/api/clients/{target}/suspend", json={"reason": "Invoice 4021 unpaid"}
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"

    detail = (await client.get(f"/api/clients/{target}")).json()
    assert detail["suspended_reason"] == "Invoice 4021 unpaid"
    assert detail["suspended_at"] is not None

    # Idempotent: a double-clicked button is not a mistake worth an error.
    assert (await client.post(f"/api/clients/{target}/suspend", json={})).status_code == 200

    restored = await client.post(f"/api/clients/{target}/unsuspend")
    assert restored.status_code == 200
    assert restored.json()["status"] == "confirmed"

    detail = (await client.get(f"/api/clients/{target}")).json()
    assert detail["suspended_reason"] is None
    assert detail["suspended_at"] is None


@pytest.mark.asyncio
async def test_unsuspend_refuses_a_live_client(client, agency_with_clients):
    target = agency_with_clients.confirmed_id
    response = await client.post(f"/api/clients/{target}/unsuspend")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_archive_from_suspended_clears_the_suspension(client, agency_with_clients):
    """A hold that becomes permanent needs no unsuspend hop — but the reason
    must not outlive the state it described."""
    target = agency_with_clients.confirmed_id
    await client.post(f"/api/clients/{target}/suspend", json={"reason": "Dispute"})

    archived = await client.post(f"/api/clients/{target}/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    detail = (await client.get(f"/api/clients/{target}")).json()
    assert detail["suspended_reason"] is None
    assert detail["suspended_at"] is None


@pytest.mark.asyncio
async def test_confirm_on_suspended_names_unsuspend(client, agency_with_clients):
    """Not "restore it before marking it confirmed" — that names an endpoint
    which would refuse this row."""
    target = agency_with_clients.confirmed_id
    await client.post(f"/api/clients/{target}/suspend", json={})

    response = await client.post(f"/api/clients/{target}/confirm")
    assert response.status_code == 400
    assert "unsuspend" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_suspended_is_a_live_client_in_the_listing(client, agency_with_clients):
    target = agency_with_clients.confirmed_id
    await client.post(f"/api/clients/{target}/suspend", json={})

    listing = (await client.get("/api/clients")).json()
    assert str(target) in [row["id"] for row in listing["items"]]
    assert listing["counts"]["suspended"] == 1
    assert listing["counts"]["all"] >= 1

    filtered = (await client.get("/api/clients?status=suspended")).json()
    assert [row["id"] for row in filtered["items"]] == [str(target)]
```

Substitute the fixture's real attribute names for `confirmed_id` / `unconfirmed_id`, and match the existing tests' HTTP client fixture and URL prefix.

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/test_clients_api.py -k "suspend" -v
```

Expected: FAIL — 404 on `/suspend`, the route does not exist.

- [ ] **Step 3: Widen the status vocabulary**

In `backend/app/api/clients.py`, line 27:

```python
StatusFilter = Literal["unconfirmed", "confirmed", "suspended", "archived", "merged"]
```

In `_serialize`, add to the returned dict (keep the existing keys):

```python
        "website": client.website,
        "phone": client.phone,
        "address": client.address,
        "fee_percent": float(client.fee_percent) if client.fee_percent is not None else None,
        "payment_terms_days": client.payment_terms_days,
        "notes": client.notes,
        "source": client.source,
        "suspended_reason": client.suspended_reason,
        "suspended_at": client.suspended_at.isoformat() if client.suspended_at else None,
```

`fee_percent` is serialised as a float because `Decimal` is not JSON-encodable by FastAPI's default encoder and the frontend renders it as a number either way.

- [ ] **Step 4: Extend the transition whitelist**

Replace `_LEGAL_SOURCES` (line 419) with:

```python
_LEGAL_SOURCES: dict[str, frozenset[str]] = {
    # `suspended` is deliberately absent here: unsuspend is the only exit from
    # a suspension, and `confirm` special-cases it below with a message that
    # names that endpoint rather than `restore`.
    Client.CONFIRMED: frozenset({Client.UNCONFIRMED, Client.CONFIRMED}),
    # Only a confirmed client can be put on hold. A client that was never
    # confirmed is not one the agency has said it works with, and putting that
    # away is what `archive` is for.
    Client.SUSPENDED: frozenset({Client.CONFIRMED, Client.SUSPENDED}),
    # From `suspended` too: a hold that becomes permanent should not need an
    # unsuspend hop first.
    Client.ARCHIVED: frozenset(
        {Client.UNCONFIRMED, Client.CONFIRMED, Client.SUSPENDED, Client.ARCHIVED}
    ),
}
```

- [ ] **Step 5: Give `_transition` an extra-values argument**

Replace `_transition` (line 425) with:

```python
async def _transition(
    request: Request,
    client_id: uuid.UUID,
    status: str,
    extra: dict | None = None,
) -> dict:
    """Move a client between statuses, optionally writing other columns with it.

    `extra` exists because suspension is not status alone: `suspend` sets the
    reason and timestamp, `unsuspend` and `archive` clear them. Doing that in
    the same UPDATE is what makes it impossible for a stale reason to outlive
    the state it describes — two statements could be interrupted between them.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status == Client.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the client first")
        if client.status not in _LEGAL_SOURCES[status]:
            # A suspended row reached through `confirm` would otherwise be told
            # to `restore` — an endpoint that refuses anything but `archived`,
            # so the caller would be sent to a second error. Name the endpoint
            # that actually works, exactly as the merged guard above does.
            if client.status == Client.SUSPENDED:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsuspend the client first, then mark it {status}",
                )
            raise HTTPException(
                status_code=400,
                detail=f"Client is {client.status}; restore it before marking it {status}",
            )
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(status=status, **(extra or {}))
        )
        await session.commit()
    return {"status": status}
```

- [ ] **Step 6: Add the two endpoints and clear the suspension on archive**

Add near the existing transitions:

```python
class SuspendRequest(BaseModel):
    """A reason is optional but wanted.

    Nothing invents one when it is absent (§15) — the row simply records that
    the client is on hold without saying why, which is the truth.
    """

    reason: str | None = None


_CLEAR_SUSPENSION = {"suspended_at": None, "suspended_reason": None}


@router.post("/clients/{client_id}/suspend")
async def suspend_client(
    request: Request, client_id: uuid.UUID, body: SuspendRequest
) -> dict:
    """Put a live client on hold — an unpaid invoice, a contract dispute.

    Distinct from `archive`, which says the agency no longer works with this
    company at all. A suspended client stays in the list and keeps its domain,
    and the matcher goes on attaching its mail; what stops is submitting
    candidates to it (see `record_submission` in app/api/sourcing.py).
    """
    return await _transition(
        request,
        client_id,
        Client.SUSPENDED,
        {"suspended_at": datetime.now(UTC), "suspended_reason": body.reason},
    )


@router.post("/clients/{client_id}/unsuspend")
async def unsuspend_client(request: Request, client_id: uuid.UUID) -> dict:
    """Lift a hold, landing back on `confirmed`.

    Deliberately unlike `restore`, which lands on `unconfirmed`. Archiving
    revoked the judgement that the agency currently works with this firm, so
    re-review is the point. A suspension revoked nothing — the agency still
    works with the firm, it simply was not sending candidates — so sending the
    row back through review would be noise.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status != Client.SUSPENDED:
            raise HTTPException(
                status_code=400,
                detail=f"Client is {client.status}, not suspended; nothing to lift",
            )
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(status=Client.CONFIRMED, **_CLEAR_SUSPENSION)
        )
        await session.commit()
    return {"status": Client.CONFIRMED}
```

Change `archive_client` (line 187) to clear the suspension as it goes:

```python
@router.post("/clients/{client_id}/archive")
async def archive_client(request: Request, client_id: uuid.UUID) -> dict:
    # Clears any suspension: a reason that outlived the state it described
    # would show a recruiter a hold on a client that is simply archived.
    return await _transition(request, client_id, Client.ARCHIVED, dict(_CLEAR_SUSPENSION))
```

Add `from datetime import UTC, datetime` to the imports.

- [ ] **Step 7: Run the tests**

```bash
uv run pytest tests/test_clients_api.py -v && uv run ruff check .
```

Expected: PASS, all of them — the pre-existing transition tests included. If `test_restore_*` now fails, `_LEGAL_SOURCES` was edited wrongly.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/clients.py backend/tests/test_clients_api.py
git commit -m "feat: suspend and unsuspend a client"
```

---

## Task 3: Create and edit a client

**Files:**
- Create: `backend/app/api/integrity.py`
- Modify: `backend/app/api/candidates.py:624-633`
- Modify: `backend/app/api/clients.py`
- Test: `backend/tests/test_clients_api.py`

**Interfaces:**
- Consumes: `Client.MANUAL`, the new columns (Task 1); `_serialize`, `_load` (existing).
- Produces: `POST /clients` (201, the serialized client), `PATCH /clients/{id}` (200, the serialized client); `app.api.integrity.is_duplicate(exc: IntegrityError) -> bool`.

- [ ] **Step 1: Hoist the duplicate check**

Create `backend/app/api/integrity.py`:

```python
"""Telling "somebody already has that" apart from a genuine fault.

Lived in `candidates.py` until `clients.py` needed the same judgement.
Importing it from a sibling endpoint module would make one endpoint depend on
another for no reason other than where the function happened to be written.
"""

from sqlalchemy.exc import IntegrityError

# Postgres SQLSTATE for a unique/exclusion violation. Only this class of
# integrity error means "somebody already has that"; every other one (a CHECK,
# a foreign key) is a different fault and must not be dressed up as a duplicate.
_UNIQUE_VIOLATION = "23505"


def is_duplicate(exc: IntegrityError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION
```

In `backend/app/api/candidates.py`, delete `_UNIQUE_VIOLATION` and `_is_duplicate` (lines 624-633) and add `from app.api.integrity import is_duplicate as _is_duplicate` to the imports. Call sites keep working unchanged.

- [ ] **Step 2: Verify nothing broke**

```bash
uv run pytest tests/test_candidates_api.py -v
```

Expected: PASS. A pure move; if anything fails, a call site was missed.

- [ ] **Step 3: Write the failing tests**

In `backend/tests/test_clients_api.py`:

```python
@pytest.mark.asyncio
async def test_create_client_starts_confirmed_and_manual(client, agency_with_clients):
    """A recruiter typing the name IS the human judgement `confirmed` records.
    Sending it to review would ask them to confirm what they just asserted."""
    response = await client.post(
        "/api/clients",
        json={
            "name": "Meridian Partners  Pte Ltd",
            "email_domain": "MERIDIAN.com.sg ",
            "fee_percent": 18.5,
            "payment_terms_days": 30,
            "notes": "Introduced by Lim",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["source"] == "manual"
    # Lowercased and stripped, so it can never miss a match on whitespace.
    assert body["email_domain"] == "meridian.com.sg"
    assert body["fee_percent"] == 18.5
    assert body["name_normalized"] == normalize_company_name("Meridian Partners  Pte Ltd")


@pytest.mark.asyncio
async def test_create_client_without_a_domain(client, agency_with_clients):
    response = await client.post("/api/clients", json={"name": "Referral Only Ltd"})
    assert response.status_code == 201
    assert response.json()["email_domain"] is None


@pytest.mark.asyncio
async def test_create_client_refuses_a_free_provider_domain(client, agency_with_clients):
    """`gmail.com` identifies a person, not a company. Storing it would claim
    the tenant's one slot for it and match every Gmail sender to this client."""
    free = next(iter(settings.FREE_EMAIL_DOMAINS))
    response = await client.post(
        "/api/clients", json={"name": "Sole Trader", "email_domain": free}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_client_names_the_domain_holder(client, agency_with_clients):
    """409, never a silent adoption: "Add client" must not sometimes mean
    "edit a row you did not know existed"."""
    existing = (await client.get(f"/api/clients/{agency_with_clients.confirmed_id}")).json()
    assert existing["email_domain"] is not None

    response = await client.post(
        "/api/clients", json={"name": "Same Firm Retyped", "email_domain": existing["email_domain"]}
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert existing["name"] in detail
    assert existing["status"] in detail


@pytest.mark.asyncio
async def test_patch_renames_and_renormalises(client, agency_with_clients):
    target = agency_with_clients.confirmed_id
    response = await client.patch(f"/api/clients/{target}", json={"name": "Acme Holdings Pte Ltd"})
    assert response.status_code == 200
    assert response.json()["name"] == "Acme Holdings Pte Ltd"
    assert response.json()["name_normalized"] == normalize_company_name("Acme Holdings Pte Ltd")


@pytest.mark.asyncio
async def test_patch_can_clear_the_domain(client, agency_with_clients):
    """A legitimate edit — "we got this wrong" — leaving the row on name-only
    matching, where every free-provider-sender row already sits."""
    target = agency_with_clients.confirmed_id
    response = await client.patch(f"/api/clients/{target}", json={"email_domain": None})
    assert response.status_code == 200
    assert response.json()["email_domain"] is None


@pytest.mark.asyncio
async def test_patch_into_a_taken_domain_is_409(client, agency_with_clients):
    holder = (await client.get(f"/api/clients/{agency_with_clients.confirmed_id}")).json()
    created = (await client.post("/api/clients", json={"name": "Other Firm"})).json()

    response = await client.patch(
        f"/api/clients/{created['id']}", json={"email_domain": holder["email_domain"]}
    )
    assert response.status_code == 409
    assert holder["name"] in response.json()["detail"]


@pytest.mark.asyncio
async def test_patch_refuses_a_merged_client(client, agency_with_clients):
    response = await client.patch(
        f"/api/clients/{agency_with_clients.merged_id}", json={"name": "Nope"}
    )
    assert response.status_code == 400
    assert "unmerge" in response.json()["detail"].lower()
```

Import at the top of the test module: `from app.core.config import settings` and `from app.services.client_naming import normalize_company_name`. Substitute the fixture's real attribute names.

- [ ] **Step 4: Run them and watch them fail**

```bash
uv run pytest tests/test_clients_api.py -k "create_client or patch" -v
```

Expected: FAIL — 405 Method Not Allowed on `POST /api/clients`.

- [ ] **Step 5: Add the request bodies and the domain rule**

In `backend/app/api/clients.py`:

```python
class _ClientFieldRules:
    """Validation shared by the create and patch bodies.

    The pipeline never stores a free-provider domain — `domain_of` returns None
    for anything in `settings.FREE_EMAIL_DOMAINS`, because such a domain
    identifies a person rather than a company. This API must not be the back
    door that puts one in.
    """

    @field_validator("email_domain", check_fields=False)
    @classmethod
    def _domain_is_a_company(cls, value: str | None) -> str | None:
        # None is meaningful and allowed: on create it means "no domain known",
        # on patch it means "clear the one we got wrong".
        if value is None:
            return None
        cleaned = value.strip().lower().lstrip("@")
        if not cleaned:
            return None
        if cleaned in settings.FREE_EMAIL_DOMAINS:
            raise ValueError(
                f"{cleaned} is a free email provider and identifies a person, "
                "not a company; leave the domain unset"
            )
        return cleaned

    @field_validator("name", check_fields=False)
    @classmethod
    def _name_is_not_blank(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class ClientCreate(_ClientFieldRules, BaseModel):
    """Only `name` is required — everything else is a fact one may not have yet."""

    name: str
    email_domain: str | None = None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    fee_percent: Decimal | None = Field(default=None, ge=0, le=100)
    payment_terms_days: int | None = Field(default=None, ge=0)
    notes: str | None = None


class ClientUpdate(_ClientFieldRules, BaseModel):
    """Every field optional — this is a PATCH.

    Reusing `ClientCreate` would make `PATCH {"notes": "..."}` a 422 for
    omitting `name`. `exclude_unset=True` on the dump is what keeps "not sent"
    different from "set to null", and that distinction only exists because
    every field may be absent here.
    """

    name: str | None = None
    email_domain: str | None = None
    website: str | None = None
    phone: str | None = None
    address: str | None = None
    fee_percent: Decimal | None = Field(default=None, ge=0, le=100)
    payment_terms_days: int | None = Field(default=None, ge=0)
    notes: str | None = None
```

Add imports: `from decimal import Decimal`, `from pydantic import BaseModel, Field, field_validator`, `from app.api.integrity import is_duplicate`, `from app.services.client_naming import normalize_company_name`.

`client_naming` imports only `re` and `settings`, so there is no cycle — and using the module the matcher itself imports is what makes the API and the pipeline normalise identically by construction.

- [ ] **Step 6: Add the collision reporter**

```python
async def _domain_conflict(tenant_uuid: uuid.UUID, domain: str) -> HTTPException:
    """Build the 409 for a domain that is already spoken for.

    The IntegrityError does not carry the holder, and the transaction that
    raised it is rolled back, so the holder is read in a fresh session. If it
    has since gone (a merge in between), the message degrades to naming the
    domain alone rather than inventing a client.
    """
    async with tenant_session(tenant_uuid) as session:
        holder = (
            await session.execute(
                select(Client).where(
                    Client.email_domain == domain, Client.status != Client.MERGED
                )
            )
        ).scalar_one_or_none()
    if holder is None:
        return HTTPException(status_code=409, detail=f"The domain {domain} is already in use")
    return HTTPException(
        status_code=409,
        detail=(
            f"{holder.name} ({holder.id}) already holds {domain} and is {holder.status}. "
            "Open that client and edit it instead of adding a second one."
        ),
    )
```

- [ ] **Step 7: Add the two endpoints**

```python
@router.post("/clients", status_code=201)
async def create_client(request: Request, body: ClientCreate) -> dict:
    """Add a client by hand, at `confirmed`.

    The pipeline's rows start at `unconfirmed` because a domain match is not a
    fact about which company an email is from. A recruiter typing the name is
    that judgement being made, so asking them to confirm it afterwards would
    be asking them to agree with themselves.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    client_id = uuid.uuid4()
    values = body.model_dump()
    values.update(
        id=client_id,
        tenant_id=tenant_uuid,
        name_normalized=normalize_company_name(body.name),
        status=Client.CONFIRMED,
        source=Client.MANUAL,
    )

    async with tenant_session(tenant_uuid) as session:
        try:
            await session.execute(insert(Client).values(**values))
            await session.commit()
        except IntegrityError as exc:
            # `uq_clients_tenant_domain` is the only constraint these values
            # can violate with a 23505: `id` is server-side generated here and
            # `status` is fixed. Anything else is a bug in this endpoint and is
            # re-raised rather than disguised as a collision.
            await session.rollback()
            if not is_duplicate(exc) or body.email_domain is None:
                raise
            raise await _domain_conflict(tenant_uuid, body.email_domain) from exc

    return await get_client(request, client_id)


@router.patch("/clients/{client_id}")
async def update_client(request: Request, client_id: uuid.UUID, body: ClientUpdate) -> dict:
    """Edit the facts a recruiter owns. Status is not among them.

    Every status change has its own endpoint, because each one is a decision
    with its own legal sources; letting PATCH write `status` would route around
    all of them.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    # exclude_unset, so an omitted field is left alone and an explicit null
    # clears the column. Those are different requests and must stay different.
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return await get_client(request, client_id)
    if "name" in changes:
        changes["name_normalized"] = normalize_company_name(changes["name"])

    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        if client.status == Client.MERGED:
            # Editing a merged row would edit something no longer in the list,
            # and an unmerge would then resurrect an edit nobody remembers.
            raise HTTPException(status_code=400, detail="Unmerge the client first")
        try:
            await session.execute(update(Client).where(Client.id == client_id).values(**changes))
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if not is_duplicate(exc) or not changes.get("email_domain"):
                raise
            raise await _domain_conflict(tenant_uuid, changes["email_domain"]) from exc

    return await get_client(request, client_id)
```

Add `insert` to the SQLAlchemy import and `from sqlalchemy.exc import IntegrityError`.

- [ ] **Step 8: Run the tests**

```bash
uv run pytest tests/test_clients_api.py -v && uv run pytest tests/test_routing.py -v && uv run ruff check .
```

Expected: PASS. `test_routing` matters here — a new route that escaped `/api` would be shadowed by the static mount.

- [ ] **Step 9: Commit**

```bash
git add backend/app/api/integrity.py backend/app/api/candidates.py backend/app/api/clients.py backend/tests/test_clients_api.py
git commit -m "feat: create and edit a client by hand"
```

---

## Task 4: Client contacts

**Files:**
- Modify: `backend/app/api/clients.py`
- Test: `backend/tests/test_clients_api.py`

**Interfaces:**
- Consumes: `ClientContact` (Task 1), `_load` (existing).
- Produces: `POST /clients/{id}/contacts` (201), `PATCH /clients/{id}/contacts/{contact_id}`, `DELETE /clients/{id}/contacts/{contact_id}` (204); `GET /clients/{id}` payload gains a `contacts` list.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_contacts_keep_exactly_one_primary(client, agency_with_clients):
    """Two contacts posted as primary in sequence: the second demotes the
    first, because `uq_client_contacts_one_primary` permits nothing else."""
    target = agency_with_clients.confirmed_id

    first = await client.post(
        f"/api/clients/{target}/contacts",
        json={"name": "Priya Menon", "title": "Head of Talent", "is_primary": True},
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/clients/{target}/contacts",
        json={"name": "Daniel Ong", "email": "daniel@example.com", "is_primary": True},
    )
    assert second.status_code == 201

    contacts = (await client.get(f"/api/clients/{target}")).json()["contacts"]
    assert len(contacts) == 2
    assert [c["name"] for c in contacts if c["is_primary"]] == ["Daniel Ong"]


@pytest.mark.asyncio
async def test_contact_patch_and_delete(client, agency_with_clients):
    target = agency_with_clients.confirmed_id
    created = (
        await client.post(f"/api/clients/{target}/contacts", json={"name": "Temp"})
    ).json()

    patched = await client.patch(
        f"/api/clients/{target}/contacts/{created['id']}",
        json={"name": "Temporary Contact", "phone": "+6591234567"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Temporary Contact"

    deleted = await client.delete(f"/api/clients/{target}/contacts/{created['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"/api/clients/{target}")).json()["contacts"] == []


@pytest.mark.asyncio
async def test_contacts_of_another_agency_are_404(client, other_agency_client_id, agency_with_clients):
    """Read, patch and delete alike. A 403 would itself disclose that the id
    exists — the same reasoning as `_load`."""
    assert (await client.get(f"/api/clients/{other_agency_client_id}")).status_code == 404
    assert (
        await client.post(f"/api/clients/{other_agency_client_id}/contacts", json={"name": "X"})
    ).status_code == 404

    contact_id = uuid.uuid4()
    assert (
        await client.patch(
            f"/api/clients/{other_agency_client_id}/contacts/{contact_id}", json={"name": "X"}
        )
    ).status_code == 404
    assert (
        await client.delete(f"/api/clients/{other_agency_client_id}/contacts/{contact_id}")
    ).status_code == 404
```

If no `other_agency_client_id` fixture exists, add one seeding a client under a second tenant and cleaning it up via `cleanup_tenant` — follow whatever `test_client_isolation.py` already does.

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/test_clients_api.py -k "contact" -v
```

Expected: FAIL — 405 on the contacts route.

- [ ] **Step 3: Add the bodies and the serializer**

```python
class ContactIn(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    is_primary: bool = False


class ContactUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    is_primary: bool | None = None


def _serialize_contact(contact: ClientContact) -> dict:
    return {
        "id": str(contact.id),
        "name": contact.name,
        "email": contact.email,
        "phone": contact.phone,
        "title": contact.title,
        "is_primary": contact.is_primary,
        "created_at": contact.created_at.isoformat(),
    }
```

- [ ] **Step 4: Add the demotion helper**

```python
async def _demote_primary(session, client_id: uuid.UUID, except_id: uuid.UUID | None) -> None:
    """Clear the existing primary before another row claims the title.

    Order is the whole safety mechanism. `uq_client_contacts_one_primary` is a
    partial unique INDEX, and an index cannot be DEFERRABLE — Postgres has no
    partial unique constraint to defer. A unique index is checked at the end of
    each statement, so demote-then-promote never has two `true` rows visible to
    a check; promote-then-demote fails mid-transaction every time.
    """
    statement = update(ClientContact).where(
        ClientContact.client_id == client_id, ClientContact.is_primary.is_(True)
    )
    if except_id is not None:
        statement = statement.where(ClientContact.id != except_id)
    await session.execute(statement.values(is_primary=False))
```

- [ ] **Step 5: Add the three endpoints**

```python
@router.post("/clients/{client_id}/contacts", status_code=201)
async def create_contact(request: Request, client_id: uuid.UUID, body: ContactIn) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    contact_id = uuid.uuid4()
    async with tenant_session(tenant_uuid) as session:
        # Loaded, not trusted: another agency's client id is a 404 here rather
        # than a foreign key violation later.
        await _load(session, client_id)
        if body.is_primary:
            await _demote_primary(session, client_id, except_id=None)
        session.add(
            ClientContact(
                id=contact_id,
                tenant_id=tenant_uuid,
                client_id=client_id,
                **body.model_dump(),
            )
        )
        await session.commit()
        contact = await _load_contact(session, client_id, contact_id)
        return _serialize_contact(contact)


@router.patch("/clients/{client_id}/contacts/{contact_id}")
async def update_contact(
    request: Request, client_id: uuid.UUID, contact_id: uuid.UUID, body: ContactUpdate
) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    changes = body.model_dump(exclude_unset=True)
    async with tenant_session(tenant_uuid) as session:
        await _load(session, client_id)
        await _load_contact(session, client_id, contact_id)
        if changes.get("is_primary"):
            await _demote_primary(session, client_id, except_id=contact_id)
        if changes:
            await session.execute(
                update(ClientContact)
                .where(ClientContact.id == contact_id)
                .values(**changes)
            )
        await session.commit()
        contact = await _load_contact(session, client_id, contact_id)
        return _serialize_contact(contact)


@router.delete("/clients/{client_id}/contacts/{contact_id}", status_code=204)
async def delete_contact(request: Request, client_id: uuid.UUID, contact_id: uuid.UUID) -> None:
    """Deleted, not archived — unlike everything else in this module.

    A contact is not evidence that something happened; it is a current fact
    about who to call. A stale one is worse than an absent one.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        await _load(session, client_id)
        await _load_contact(session, client_id, contact_id)
        await session.execute(delete(ClientContact).where(ClientContact.id == contact_id))
        await session.commit()


async def _load_contact(session, client_id: uuid.UUID, contact_id: uuid.UUID) -> ClientContact:
    """404 for a contact that is not this client's, for the same reason `_load`
    404s across tenants: an id that exists but is not yours is a disclosure."""
    contact = (
        await session.execute(
            select(ClientContact).where(
                ClientContact.id == contact_id, ClientContact.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact
```

- [ ] **Step 6: Return contacts from the detail endpoint**

In `get_client` (line 116), after the mentions query:

```python
        contacts = (
            (
                await session.execute(
                    select(ClientContact)
                    .where(ClientContact.client_id == client_id)
                    # Primary first, then oldest first — a stable order, so two
                    # readers of the same client see the same list.
                    .order_by(ClientContact.is_primary.desc(), ClientContact.created_at)
                )
            )
            .scalars()
            .all()
        )
```

and before the return:

```python
    payload["contacts"] = [_serialize_contact(c) for c in contacts]
```

Add `ClientContact` to the model import.

- [ ] **Step 7: Run the tests**

```bash
uv run pytest tests/test_clients_api.py -v && uv run ruff check .
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/clients.py backend/tests/test_clients_api.py
git commit -m "feat: client contacts with a single primary"
```

---

## Task 5: Suspension blocks submissions, and the matcher ignores it

**Files:**
- Modify: `backend/app/api/sourcing.py:274` (`record_submission`)
- Test: `backend/tests/test_sourcing_api.py` (or whichever module covers `record_submission` — find it with `grep -rln "submissions" backend/tests/`)
- Test: `backend/tests/test_client_matching.py`

**Interfaces:**
- Consumes: `Client.SUSPENDED`, `Client.suspended_reason` (Tasks 1-2).
- Produces: no new symbols. `POST /candidates/{id}/submissions` now answers 409 for a suspended client.

- [ ] **Step 1: Write the failing tests**

In the submissions test module:

```python
@pytest.mark.asyncio
async def test_submission_to_a_suspended_client_is_refused(client, ...):
    """Submitting is the outward-facing act, and the one that must not happen
    while a client is on hold."""
    await client.post(f"/api/clients/{client_id}/suspend", json={"reason": "Invoice 4021 unpaid"})

    response = await client.post(
        f"/api/candidates/{candidate_id}/submissions",
        json={"client_id": str(client_id), "opportunity_id": str(opportunity_id)},
    )
    assert response.status_code == 409
    # The reason, not a generic failure — the recruiter must be able to act on it.
    assert "Invoice 4021 unpaid" in response.json()["detail"]


@pytest.mark.asyncio
async def test_sourcing_still_runs_for_a_suspended_client(client, ...):
    """Ranking is internal research. Blocking it would stop a recruiter
    preparing for the day the hold lifts."""
    await client.post(f"/api/clients/{client_id}/suspend", json={})
    response = await client.post(f"/api/opportunities/{opportunity_id}/sourcing", json={})
    assert response.status_code in (200, 201)
```

Fill the fixture arguments and request bodies from the neighbouring submission tests in that file — do not invent a payload shape.

In `backend/tests/test_client_matching.py`:

```python
@pytest.mark.asyncio
async def test_matcher_leaves_a_suspended_client_suspended(...):
    """Suspension is a commercial state, not an identity one.

    `_BY_DOMAIN` matches any non-deprioritised status and `_surviving` never
    writes `status`, so an inbound email touches `last_seen_at` and nothing
    else. The same reasoning as the archived-row comment in models/client.py.
    """
    # Seed a suspended client on a known domain, run match_client for a sender
    # at that domain, then assert:
    assert matched.id == suspended_client_id
    assert matched.status == "suspended"
    assert matched.last_seen_at is not None
```

Follow the existing tests in that module for how they seed a client and invoke `match_client`.

- [ ] **Step 2: Run them and watch them fail**

```bash
uv run pytest tests/test_client_matching.py -k suspended -v
```

Expected: the matching test FAILs only if the matcher is wrong — it may well pass immediately. That is the point: the test pins behaviour the spec asserts is already correct, so a future change to `_BY_DOMAIN` cannot silently break it. The submission test must FAIL (201, not 409).

- [ ] **Step 3: Add the guard**

In `backend/app/api/sourcing.py`, inside `record_submission` immediately after the existing client 404 check:

```python
        if client.status == Client.SUSPENDED:
            # A hold is a commercial decision, and putting a candidate in front
            # of the client is the act it exists to stop. Sourcing and ranking
            # for the same client stay open — see the design note in
            # docs/superpowers/specs/2026-07-30-clients-administration-design.md.
            #
            # The reason is echoed rather than summarised: "this client is
            # suspended" sends the recruiter hunting for why, and the why is
            # already stored.
            detail = f"{client.name} is suspended"
            if client.suspended_reason:
                detail = f"{detail}: {client.suspended_reason}"
            raise HTTPException(status_code=409, detail=detail)
```

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_client_matching.py tests/test_sourcing_api.py -v && uv run ruff check .
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/sourcing.py backend/tests
git commit -m "feat: refuse a submission to a suspended client"
```

---

## Task 6: Frontend API wrappers

**Files:**
- Modify: `frontend/app/dashboard/clients.ts`
- Test: `frontend/app/dashboard/clients.test.ts` (create if absent — `candidates.test.ts` is the model)

**Interfaces:**
- Consumes: every endpoint from Tasks 2-4.
- Produces: `createClient(body: ClientInput): Promise<Client>`, `updateClient(id: string, body: Partial<ClientInput>): Promise<Client>`, `suspendClient(id: string, reason?: string): Promise<void>`, `unsuspendClient(id: string): Promise<void>`, `createContact(clientId, body: ContactInput): Promise<Contact>`, `updateContact(clientId, contactId, body: Partial<ContactInput>): Promise<Contact>`, `deleteContact(clientId, contactId): Promise<void>`; types `Client` (widened), `ClientInput`, `Contact`, `ContactInput`.

- [ ] **Step 1: Widen the types**

In `frontend/app/dashboard/clients.ts`, extend the existing `Client` type to match `_serialize` exactly:

```ts
export type ClientStatus =
  | "unconfirmed"
  | "confirmed"
  | "suspended"
  | "archived"
  | "merged";

export type Contact = {
  id: string;
  name: string;
  email: string | null;
  phone: string | null;
  title: string | null;
  is_primary: boolean;
  created_at: string;
};

export type ContactInput = {
  name: string;
  email?: string | null;
  phone?: string | null;
  title?: string | null;
  is_primary?: boolean;
};

export type ClientInput = {
  name: string;
  email_domain?: string | null;
  website?: string | null;
  phone?: string | null;
  address?: string | null;
  fee_percent?: number | null;
  payment_terms_days?: number | null;
  notes?: string | null;
};
```

Add to the existing `Client` type: `website`, `phone`, `address` (`string | null`), `fee_percent`, `payment_terms_days` (`number | null`), `notes`, `suspended_reason`, `suspended_at` (`string | null`), `source` (`"pipeline" | "manual"`), and change `status` to `ClientStatus`. The detail response type additionally carries `contacts: Contact[]`.

- [ ] **Step 2: Add the wrappers**

Following the exact shape of the existing `confirmClient` / `mergeClient` wrappers in the same file (same fetch helper, same error handling, same revalidation):

```ts
export async function createClient(body: ClientInput): Promise<Client> { /* POST /clients */ }
export async function updateClient(id: string, body: Partial<ClientInput>): Promise<Client> { /* PATCH /clients/{id} */ }
export async function suspendClient(id: string, reason?: string): Promise<void> { /* POST /clients/{id}/suspend, body {reason} */ }
export async function unsuspendClient(id: string): Promise<void> { /* POST /clients/{id}/unsuspend */ }
export async function createContact(clientId: string, body: ContactInput): Promise<Contact> { /* POST /clients/{id}/contacts */ }
export async function updateContact(clientId: string, contactId: string, body: Partial<ContactInput>): Promise<Contact> { /* PATCH */ }
export async function deleteContact(clientId: string, contactId: string): Promise<void> { /* DELETE */ }
```

Fill each body from the neighbouring wrapper it mirrors. Do not introduce a second fetch idiom in this file.

- [ ] **Step 3: Surface the 409 detail**

Whatever error type the existing wrappers throw must carry the server's `detail` string through for `createClient`, `updateClient` and `suspendClient` — the domain-collision and suspension messages are written to be read by a recruiter, and a wrapper that replaces them with "Request failed" throws away the whole point. If the existing helper already does this, change nothing.

- [ ] **Step 4: Write the tests**

In `frontend/app/dashboard/clients.test.ts`, mirroring `candidates.test.ts`: assert `createClient` posts to the right path with the right body, that `updateClient` sends only the keys it was given (a `Partial`, not a full object with undefineds), and that a 409 response surfaces its `detail`.

- [ ] **Step 5: Run**

```bash
cd frontend && npx vitest run app/dashboard/clients.test.ts && npx tsc --noEmit
```

Expected: PASS, and no type errors — `tsc` is what proves the widened `Client` type matches every existing consumer.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/dashboard/clients.ts frontend/app/dashboard/clients.test.ts
git commit -m "feat: client administration API wrappers"
```

---

## Task 7: The dashboard

**Files:**
- Create: `frontend/app/dashboard/clients/client-form.tsx`
- Modify: `frontend/app/dashboard/clients/page.tsx`, `clients-table.tsx`, `client-panel.tsx`

**Interfaces:**
- Consumes: everything from Task 6.
- Produces: `<ClientForm client={existing ?? null} onDone={() => void} />` — one component, create when `client` is null, edit otherwise.

- [ ] **Step 1: Read the model before writing anything**

```bash
cat frontend/app/dashboard/candidates/candidate-form.tsx frontend/app/dashboard/dialog.tsx
```

`client-form.tsx` follows that form's structure, validation and submit handling. `client-panel.tsx` is 455 lines and must not absorb the form — that is why this is a separate file.

- [ ] **Step 2: Build the form**

`client-form.tsx` renders, in this order: name (required), email domain, website, phone, address, fee percent, payment terms days, notes, then a contacts section listing existing contacts with inline add and remove and a single "primary" radio across the group.

Three rules the backend enforces and the form must not contradict:

1. A free-provider email domain is a 422. Show the server's message against the domain field rather than as a page-level error.
2. A 409 on save means another client holds the domain. Show the server's `detail` verbatim — it names the client and tells the recruiter what to do.
3. On edit, send only changed fields (`PATCH` semantics). Sending the whole object would turn every untouched empty input into an explicit null and wipe fields the recruiter never looked at.

Contacts are saved through their own endpoints, after the client exists — on create, the client is POSTed first and its id used for the contact calls.

- [ ] **Step 3: Wire the entry points**

- `page.tsx`: an **Add client** button beside the status chips, opening `<ClientForm client={null} />` in the existing `dialog.tsx`; add `suspended` to the status chip row, reading its count from `counts.suspended`.
- `clients-table.tsx`: a suspended badge — amber, visibly distinct from archived's muted grey.
- `client-panel.tsx`: a suspension banner at the top when `status === "suspended"`, showing `suspended_reason` and the date; a read-only **Details** section (website, phone, address, fee percent, payment terms, notes, contacts with the primary marked) with an **Edit** button opening `<ClientForm client={client} />`; **Suspend** (prompting for an optional reason) and **Unsuspend** in the action row, shown only for the statuses that accept them — Suspend on `confirmed`, Unsuspend on `suspended`.

- [ ] **Step 4: Surface the submission 409**

Wherever the submissions UI calls `POST /candidates/{id}/submissions`, render the 409 `detail` as the message. It already carries the client name and the suspension reason.

- [ ] **Step 5: Build and typecheck**

```bash
cd frontend && npx tsc --noEmit && npm run build
```

Expected: no type errors, static export succeeds.

- [ ] **Step 6: Verify against the running app**

Start the backend (`uv run uvicorn app.main:app --reload`) and the frontend dev server, then walk it: add a client; add one with a domain that already exists and read the 409; edit the fee; add two contacts and confirm only the second is primary; suspend with a reason and see the banner; try a submission to that client and read the refusal; unsuspend and see it return to confirmed.

Paste the actual output — a screenshot or the observed messages. Do not report this step as done from the code alone.

- [ ] **Step 7: Commit**

```bash
git add frontend/app/dashboard/clients
git commit -m "feat: administer clients from the dashboard"
```

---

## Final verification

- [ ] **Full backend suite**

```bash
cd backend && uv run pytest && uv run ruff check .
```

Expected: all green. Quote the summary line.

- [ ] **Migration round-trips**

```bash
cd backend && uv run alembic downgrade -1 && uv run alembic upgrade head
```

Expected: both succeed. A downgrade that fails means `client_contacts` or a column drop was left out.

- [ ] **Routing and RLS backstops**

```bash
cd backend && uv run pytest tests/test_routing.py tests/test_rls.py -v
```

Expected: PASS. These two catch the failures that would otherwise only appear in production — a route shadowed by the static mount, and a new table readable across tenants.

- [ ] **Frontend**

```bash
cd frontend && npx tsc --noEmit && npm run build && npx vitest run
```

Expected: all green.
