# Linking a Job Order to Its Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a recruiter say which client an existing job order came from, so that job orders whose source email named several companies (or none) can be linked and routed.

**Architecture:** One new backend route guarded by a rule of its own — editable **or unassigned**, because every row this exists to fix is unassigned. One extracted frontend search component, reused by the detail panel and by the manual-creation form so there is one implementation rather than two.

**Tech Stack:** Backend FastAPI / SQLAlchemy 2.0 async / pytest. Frontend Next.js 15 static export, React 19, plain hooks, Vitest + Testing Library + happy-dom.

**Spec:** [2026-07-31-linking-a-job-order-to-its-client-design.md](../specs/2026-07-31-linking-a-job-order-to-its-client-design.md)

## Global Constraints

- Backend from `backend/`: `uv run pytest`, `uv run ruff check .`. Frontend from `frontend/`: `npm test` (which is `tsc --noEmit && vitest run`), `npm run build`.
- **No new frontend dependencies.** Runtime deps stay `next`, `react`, `react-dom`, `qrcode`.
- **There is no `@testing-library/jest-dom` and no `@testing-library/user-event`**, and `vitest.config.ts` has no `setupFiles`. `toBeInTheDocument`, `toHaveTextContent`, `toBeDisabled`, `toBeChecked` and `userEvent.*` **do not exist** and fail to compile. Assert in the house style of `app/dashboard/clients/client-form.test.tsx`: `expect(el).toBeNull()` / `not.toBeNull()`, `expect(el.textContent).toContain(…)`, `expect((el as HTMLInputElement).checked).toBe(true)`; drive inputs with `fireEvent`.
- **No hardcoded values** — backend config via `app.core.config.settings`.
- Every backend route lives under `/api` — `backend/tests/test_routing.py` enforces it.
- Every by-id read of an `Opportunity` in `app/api/` goes through `load_visible_opportunity` / `load_editable_opportunity` — `backend/tests/test_opportunity_routes_guarded.py` enforces it **transitively, through module-level helpers**, so a route that delegates is still caught.
- **Do not modify `app/services/visibility.py`** — it is verified and mutation-tested.
- No single file exceeds 1500 LOC. Current: `app/api/opportunities.py` 1014, `app/api/clients.py` 1157, `app/dashboard/detail-panel.tsx` ~371, `app/dashboard/job-order-form.tsx` 322, `app/app.css` 1283, `app/dashboard/job-orders.css` 615.
- New job-order CSS goes in `app/dashboard/job-orders.css`, never `app.css`.
- Sentence case in all UI copy. No "successfully", no "please", no exclamation marks.

## Baseline

Backend 1606 pytest passing; frontend 97 tests across 17 files; `npm run build` green.

---

### Task 1: The endpoint

**Files:**
- Modify: `backend/app/api/opportunities.py` (add a request model and one route)
- Test: `backend/tests/test_opportunity_client_link.py` (create)

**Interfaces:**
- Produces: `POST /api/opportunities/{id}/client`, body `{client_id: uuid | null, adopt_client_recruiter: bool = true}`, returning `{"id", "client_id", "assigned_user_id", "assignee_name"}`. Task 3 consumes it.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_opportunity_client_link.py`. Follow the seeding style in `tests/test_opportunity_visibility_routes.py`; helpers `seed_tenant_with_user`, `AdminSessionLocal`, `cleanup_tenant` come from `tests.conftest`.

Write all nine out fully:

```python
"""Saying which client a job order came from.

Every row this exists to fix is unassigned, which is why the permission rule
here is "editable OR unassigned" rather than `can_edit` alone — `can_edit`
deliberately refuses unassigned rows, and gating on it would make the endpoint
unable to solve its own problem.
"""


async def test_linking_a_client_adopts_its_recruiter() -> None:
    # Unassigned job order + a client assigned to Wei Kian -> the job order
    # becomes Wei Kian's, and the response names them.
    ...


async def test_adopt_false_leaves_it_on_the_queue() -> None:
    ...


async def test_a_client_with_no_recruiter_leaves_it_unassigned() -> None:
    ...


async def test_an_assigned_job_order_never_changes_hands() -> None:
    """Linking a client is not a way to take someone else's work."""
    # Assigned to A; A links a client owned by B; assignee stays A even with
    # adopt_client_recruiter=true.
    ...


async def test_the_assignee_may_set_the_client_on_their_own_job_order() -> None:
    ...


async def test_a_bystander_is_refused_and_writes_nothing() -> None:
    """A permission check placed after the update would leave the damage done."""
    # B's assigned job order, C calls the route -> 403 AND client_id unchanged
    # in the database.
    ...


async def test_an_invisible_job_order_is_404_not_403() -> None:
    """A 403 would confirm the row exists."""
    ...


async def test_null_unlinks_and_does_not_touch_the_assignee() -> None:
    ...


async def test_adoption_notifies_the_new_owner() -> None:
    """A job order quietly becoming yours is what the assigned event is for."""
    # Linking a client whose recruiter is B, on an unassigned job order, emits
    # EVENT_OPPORTUNITY_ASSIGNED naming only B.
    ...


async def test_linking_without_adoption_notifies_nobody() -> None:
    # Nothing changed hands, so there is nothing to announce.
    ...


async def test_a_client_from_another_agency_is_refused() -> None:
    # 422 before the composite FK can turn it into a 500 — the same guard
    # `assign_opportunity` puts on its user target.
    ...
```

- [ ] **Step 2: Run and watch them fail**

```bash
uv run pytest tests/test_opportunity_client_link.py -v
```

Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Add the request model**

Beside the existing `AssignRequest` in `app/api/opportunities.py`:

```python
class ClientLinkRequest(BaseModel):
    client_id: uuid.UUID | None
    # Defaults true: linking a client should produce the same outcome the
    # pipeline would have produced had the link been there from the start —
    # a job order goes to the client's recruiter. It stays a flag because
    # linking a client and taking ownership are two different intentions.
    adopt_client_recruiter: bool = True
```

- [ ] **Step 4: Write the route**

Model it on `assign_opportunity` (around line 859), which already carries the two things this needs: a guard of its own beyond the generic loader, and a pre-check that refuses a foreign target before the composite FK turns it into a 500.

```python
@router.post("/opportunities/{opportunity_id}/client")
async def set_opportunity_client(
    opportunity_id: uuid.UUID, body: ClientLinkRequest, request: Request
) -> dict:
    """Say which client this job order came from.

    Guarded by "editable OR unassigned", not by `load_editable_opportunity`.
    `can_edit` refuses unassigned rows on purpose — claiming is what makes a
    job order editable — but recording which company a job order came from is
    a factual correction rather than an act of ownership, and the queue is
    shared work. Someone else's *assigned* job order stays closed.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        opportunity = await load_visible_opportunity(
            session, opportunity_id, user_uuid, role
        )
        if not (
            opportunity.assigned_user_id is None
            or can_edit(opportunity, user_uuid, role)
        ):
            raise HTTPException(
                status_code=403,
                detail="This job order is shared with you, not assigned to you.",
            )

        # Read off the row while the session is open: after commit a
        # committed instance's attributes are expired, and the event below
        # needs these. Same ordering argument as `assign_opportunity`.
        previous_assignee = opportunity.assigned_user_id
        subject_title = opportunity.job_title_raw
        subject_company = opportunity.company_name_raw
        subject_location = opportunity.location_raw
        subject_salary = opportunity.salary_raw

        adopted: uuid.UUID | None = opportunity.assigned_user_id
        if body.client_id is not None:
            # RLS scopes this to the agency, so a client of another agency is
            # simply not found — refused here rather than as a 500 from the
            # composite foreign key.
            client = (
                await session.execute(
                    select(Client.id, Client.assigned_user_id).where(
                        Client.id == body.client_id
                    )
                )
            ).one_or_none()
            if client is None:
                raise HTTPException(
                    status_code=422, detail="That client is not in this agency."
                )
            # Only an unassigned job order adopts. An assigned one never
            # changes hands here — that is what the assign route is for.
            if (
                body.adopt_client_recruiter
                and opportunity.assigned_user_id is None
                and client.assigned_user_id is not None
            ):
                adopted = client.assigned_user_id

        await session.execute(
            update(Opportunity)
            .where(Opportunity.id == opportunity_id)
            .values(client_id=body.client_id, assigned_user_id=adopted)
        )

        name = None
        if adopted is not None:
            name = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.nullif(func.btrim(User.preferred_name), ""),
                            func.nullif(func.btrim(User.display_name), ""),
                            func.split_part(User.email, "@", 1),
                        )
                    ).where(User.id == adopted)
                )
            ).scalar_one_or_none()

    # A job order quietly becoming yours is exactly what the assigned event
    # exists to announce. `assign_opportunity` emits it; this route assigns
    # too, so it must as well, or adoption is the one way to gain work without
    # being told. Emitted after the transaction commits, matching the ordering
    # `opportunity_shares.py` uses and explains.
    if adopted is not None and adopted != previous_assignee:
        await emit_and_enqueue(
            OpportunityEvent(
                kind=EVENT_OPPORTUNITY_ASSIGNED,
                tenant_id=tenant_uuid,
                opportunity_id=opportunity_id,
                recipient_user_ids=(adopted,),
                job_title=subject_title,
                company_name=subject_company,
                location=subject_location,
                salary=subject_salary,
            )
        )

    return {
        "id": str(opportunity_id),
        "client_id": str(body.client_id) if body.client_id else None,
        "assigned_user_id": str(adopted) if adopted else None,
        "assignee_name": name,
    }
```

**Two import facts, both verified — the code above will not run without them:**

1. **`can_edit` is not imported.** `app/api/opportunities.py:51-56` imports only `load_editable_opportunity`, `load_visible_opportunity`, `shared_with_me_exists` and `visible_opportunities` from `app.services.visibility`. Add `can_edit` to that list. Import `Client` too if it is not already present.

2. **`_assignee_name_expr` lives in `app/api/clients.py:110`, not here.** Do NOT import it — `opportunities.py` importing from another API module is the wrong direction, and a previous change deliberately declined to do it. `list_opportunities` builds the same expression inline, with a comment at around line 236 cross-referencing the clients one. Reuse **that inline expression from this same module** for the response's `assignee_name` — extract it to a module-level helper in `opportunities.py` if that reads better, which keeps it to the two copies the codebase already accepted rather than adding a third here.

The chain is `preferred_name` → `display_name` → email local-part, each wrapped so a whitespace-only value falls through. Getting it wrong would make this route report a name that disagrees with the one the list shows for the same person.

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/test_opportunity_client_link.py -v
```

Expected: 9 passed.

- [ ] **Step 6: Confirm the structural guard still passes**

```bash
uv run pytest tests/test_opportunity_routes_guarded.py -v
```

Expected: **pass, with no change to that file.** This was checked against the test rather than guessed: `tests/test_opportunity_routes_guarded.py:253-266` counts a call to `can_edit` as satisfying the mutating-route assertion (`EDIT_CHECK`), and `load_visible_opportunity` satisfies the read assertion. The planned route calls both, so it passes as written.

**Do not add an `EXEMPT` entry.** If you find yourself reaching for one, something else is wrong — that map is the structural net for every future route, and an entry added to quiet a passing test is the beginning of the leak it exists to prevent.

- [ ] **Step 7: Full suite, lint, size**

```bash
uv run pytest -q && uv run ruff check . && wc -l app/api/opportunities.py
```

Expected: green; the file stays under 1500.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api/opportunities.py backend/tests/test_opportunity_client_link.py
git commit -m "Let a job order be told which client it came from"
```

---

### Task 2: One client search, not two

**Files:**
- Create: `frontend/app/dashboard/client-search.tsx`, `frontend/app/dashboard/client-search.test.tsx`
- Modify: `frontend/app/dashboard/job-order-form.tsx` (use the extracted component)

**Interfaces:**
- Produces:

```tsx
export function ClientSearch({ value, onChange, label }: {
  value: { id: string; name: string } | null;
  onChange: (client: { id: string; name: string } | null) => void;
  label: string;
}): JSX.Element
```

Task 3 consumes it.

- [ ] **Step 1: Write the failing test**

`frontend/app/dashboard/client-search.test.tsx`:

```tsx
it("searches as you type rather than preloading", async () => {
  // Clients are paginated and an agency accumulates hundreds; this is why it
  // is a search and not a dropdown.
  fireEvent.change(screen.getByLabelText("Client"), { target: { value: "sun" } });
  await waitFor(() => expect(lastUrl(fetchMock)).toContain("q=sun"));
});

it("debounces rather than firing per keystroke", async () => {
  // Type three characters in quick succession; only one request should go out.
  // The existing test for this behaviour passes equally with one request per
  // keystroke, which is why this one counts calls.
  ...
  expect(fetchMock.mock.calls.filter(isClientSearch).length).toBe(1);
});

it("reports the chosen client", async () => { ... });

it("clears back to no client", async () => {
  // A job order taken over the phone from an unrecorded company legitimately
  // has none.
  ...
});

it("says so when the search cannot be read", async () => { ... });
```

The debounce test is the one worth care: the existing manual-creation test passes whether or not the debounce works, which is a gap this extraction is a good moment to close.

- [ ] **Step 2: Run and watch it fail**

```bash
npx vitest run app/dashboard/client-search.test.tsx
```

Expected: FAIL — the module does not exist.

- [ ] **Step 3: Extract**

Move the client-search block out of `job-order-form.tsx` — the debounce constant at line 34, the limit at 39, the `URLSearchParams` build at 98, and the surrounding state and effect — into `client-search.tsx`, unchanged in behaviour. Then have `job-order-form.tsx` render `<ClientSearch>` instead of its own copy.

`job-order-form.tsx` currently declares `CLIENT_SEARCH_DEBOUNCE_MS = 300` as a second literal alongside a module-private `SEARCH_DEBOUNCE_MS` in `opportunities.ts`. Now that one component owns the search, declare the constant once in `client-search.tsx` and delete the duplicate.

- [ ] **Step 4: Run the tests, including the manual form's**

```bash
npx vitest run app/dashboard/client-search.test.tsx app/dashboard/job-order-form.test.tsx
```

Expected: both pass. The manual-creation tests must pass **unchanged** — if you had to edit them, the extraction changed behaviour and you should say what changed and why in your report.

- [ ] **Step 5: Whole suite, build, commit**

```bash
npm test && npm run build
git add frontend/app/dashboard/client-search.tsx frontend/app/dashboard/client-search.test.tsx frontend/app/dashboard/job-order-form.tsx
git commit -m "Give the client search one home"
```

---

### Task 3: The client field on the detail panel

**Files:**
- Modify: `frontend/app/dashboard/detail-panel.tsx`
- Modify: `frontend/app/dashboard/job-orders.tsx` — **required, and easy to miss.** `detail-panel.tsx:60-69` receives only `row, onReview, onClaim, onAssign, onVanished`; `patchRow` and `setSelected` live in `job-orders.tsx` (around lines 88, 116, 341). Step 4 needs a new `onClientSet` prop threaded through, exactly as `onClaim` already is.
- Modify: `frontend/app/dashboard/opportunity-actions.ts` (add the mutation)
- Modify: `frontend/app/api.ts` (add the path helper)
- Modify: `frontend/app/dashboard/job-orders.css`
- Test: `frontend/app/dashboard/detail-panel-client.test.tsx` (create)

**Interfaces:**
- Consumes: `ClientSearch` (Task 2); the endpoint from Task 1.
- Produces: `setOpportunityClient(id, clientId, adopt)` returning the same `MutationResult` shape the other mutations use — `{ ok: true; … } | { ok: false; kind: "conflict" | "gone" | "forbidden" | "denied" | "failed"; message: string }`.

- [ ] **Step 1: Write the failing tests**

```tsx
it("says a job order is not linked to a client", async () => {
  // The company name from the AI extraction sits directly above this field,
  // so a blank field alone implies the opposite of the truth.
  ...
  expect(screen.queryByText(/not linked to a client/i)).not.toBeNull();
});

it("does not say that when a client is linked", async () => { ... });

it("posts the chosen client with adopt defaulted on", async () => {
  expect(lastBody(fetchMock)).toEqual({
    client_id: "c-1",
    adopt_client_recruiter: true,
  });
});

it("sends adopt false when the checkbox is cleared", async () => { ... });

it("shows who the job order went to after linking", async () => {
  // The response names the recruiter so ownership does not change silently.
  expect((await screen.findByText(/wei kian/i))).not.toBeNull();
});

it("updates the list row too, not just the panel", async () => {
  // `patchRow` exists for exactly this; without it the list shows a stale
  // owner beside a correct panel.
  ...
});

it("renders a 403 as the shared-not-assigned sentence", async () => { ... });

it("closes the panel on a 404", async () => { ... });
```

- [ ] **Step 2: Run and watch them fail**

```bash
npx vitest run app/dashboard/detail-panel-client.test.tsx
```

- [ ] **Step 3: Add the path helper and the mutation**

In `frontend/app/api.ts`, following the `{entity}{Action}Path(id)` convention already used by `opportunityClaimPath` and `opportunityAssignPath`:

```tsx
export const opportunityClientPath = (id: string) =>
  `${API_BASE}/api/opportunities/${encodeURIComponent(id)}/client`;
```

In `opportunity-actions.ts`, add `setOpportunityClient` alongside `claimOpportunity` and `assignOpportunity`, using the same `mutate` helper so the `kind` discriminant and the status→message mapping are shared rather than re-derived.

- [ ] **Step 4: Wire the panel**

Add the client row to `detail-panel.tsx`: `<ClientSearch>`, an "Also take on this client's recruiter" checkbox defaulted checked and shown only while the job order is unassigned, and the unlinked line when `row.client_id` is null.

On success, call the same `patchRow`/`setSelected` pair the claim path already uses, so the list row and the panel agree — a previous task shipped a bug where they did not, and the fix is the pattern to copy.

- [ ] **Step 5: Tests, whole suite, build**

```bash
npx vitest run app/dashboard/detail-panel-client.test.tsx && npm test && npm run build
```

- [ ] **Step 6: Commit**

```bash
git add frontend/app/dashboard/ frontend/app/api.ts
git commit -m "Let a recruiter link a job order to its client from the panel"
```

---

## Deployment note

No migration — `opportunities.client_id` already exists in production and is nullable. Merging to `main` triggers CI/CD, which runs `alembic upgrade head` (a no-op here) and redeploys.

**After deploying, this is the manual step that actually fixes the eight rows:** open each unlinked job order, pick its client, and leave the adopt checkbox on. Five of them have six candidate companies in their source email, which is exactly why a person has to choose.

## Self-review

**Spec coverage.** The endpoint and its permission rule → Task 1. Adoption and the never-changes-hands rule → Task 1. The extracted search → Task 2. The panel field, the adopt checkbox and the unlinked line → Task 3. Every test named in the spec maps to a task.

**Two things found while writing:**

1. The route calls `load_visible_opportunity` and then applies a wider rule, so the structural guard test may flag it as a route that reads by id without the edit guard. Task 1 Step 6 handles that explicitly rather than leaving the implementer to discover it and guess whether to weaken the test.
2. Four things a review caught before execution: Task 3 did not list `job-orders.tsx`, though the panel has no `patchRow` prop and one must be threaded; the route snippet called a helper (`_assignee_name_expr`) that exists only in `clients.py` and is built inline here against a query alias, so the expression is now written out against `User` directly; Step 6's guard-test hedge is resolved definitively (the test counts `can_edit`, so no exemption is needed and adding one would be wrong); and adoption assigned a job order **without emitting `EVENT_OPPORTUNITY_ASSIGNED`**, making it the one way to gain work without being told — now emitted, with two tests.

3. Two import facts corrected against the real file: `can_edit` is **not** currently imported into `opportunities.py`, and `_assignee_name_expr` lives in `clients.py`, not there. Following the plan as first written would have produced a `NameError` on the first request. Task 1 Step 4 now states both explicitly, and says to reuse the inline expression already in `opportunities.py` rather than importing across API modules.
