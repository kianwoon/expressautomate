# Job Order Sharing UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make job order assignment and sharing visible and usable — a recruiter can see who owns each job order, work a claimable queue, share to colleagues or the agency, assign clients to recruiters, and type in a job order taken over the phone.

**Architecture:** Three additive API changes first (payload fields, a members endpoint, a `?scope=` filter), then frontend built on the house pattern — plain hooks, per-domain modules, `fetch` with `credentials: "include"`, no new dependencies. One shared `member-picker` serves sharing, job-order assignment and client assignment.

**Tech Stack:** Backend FastAPI / SQLAlchemy 2.0 async / pytest. Frontend Next.js 15 static export, React 19, plain hooks, Vitest + Testing Library + happy-dom, hand-rolled CSS with tokens.

**Spec:** [2026-07-31-job-order-sharing-ui-design.md](../specs/2026-07-31-job-order-sharing-ui-design.md)

## Global Constraints

- Backend commands from `backend/`: `uv run pytest`, `uv run ruff check .`. Frontend from `frontend/`: `npm test` (which is `tsc --noEmit && vitest run`).
- **No new frontend dependencies.** Runtime deps stay `next`, `react`, `react-dom`, `qrcode`. No component library, no state library, no CSS framework.
- **No hardcoded values.** Backend config via `app.core.config.settings`; frontend colours via CSS custom properties in `app/globals.css`.
- Every backend route lives under `/api` — `backend/tests/test_routing.py` enforces it.
- Every by-id read of an `Opportunity` in `app/api/` goes through `load_visible_opportunity` / `load_editable_opportunity` — `backend/tests/test_opportunity_routes_guarded.py` enforces it transitively, including through module-level helpers.
- No single file exceeds 1500 LOC. Current sizes to watch: `app/api/opportunities.py` 952, `app/api/clients.py` 1000, `frontend/app/dashboard/clients/client-panel.tsx` 698, `frontend/app/dashboard/opportunities.ts` 557.
- Sentence case in all UI copy. No "successfully", no "please", no exclamation marks.
- Frontend path helpers follow `{entity}{Action}Path(id)` and live in `frontend/app/api.ts`.
- Tests colocate with the component as `*.test.tsx`.

---

## Phase A — the API gaps

### Task 1: Expose ownership on the opportunity payload

**Files:**
- Modify: `backend/app/api/opportunities.py` — `_payload`, and its call sites in `list_opportunities`
- Test: `backend/tests/test_opportunity_payload_fields.py` (create)

**Interfaces:**
- Produces: five new keys on every opportunity JSON object — `assigned_user_id: str | null`, `assignee_name: str | null`, `client_id: str | null`, `source: "pipeline" | "manual"`, `shared_with_me: bool`. Tasks 6, 7 and 8 consume all five.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_opportunity_payload_fields.py`. Reuse `seed_tenant_with_user`, `AdminSessionLocal` and `cleanup_tenant` from `tests.conftest`, and the `sign_in` cookie helper pattern from `tests/test_opportunity_visibility_routes.py`.

```python
"""The list has to say who owns a row, or the interface cannot show it."""


async def test_the_list_reports_the_assignee_and_their_name() -> None:
    tenant_id, mine = await seed_tenant_with_user()
    # ... seed an opportunity assigned to `mine`, sign in as `mine`
    body = (await client.get("/api/opportunities")).json()
    row = body["items"][0]
    assert row["assigned_user_id"] == str(mine)
    assert row["assignee_name"] is not None
    assert row["source"] == "manual"
    assert row["shared_with_me"] is False


async def test_an_unassigned_row_reports_a_null_assignee() -> None:
    # assigned_user_id is None -> both id and name are null, and the row is
    # still returned: the queue is visible to everyone.
    ...


async def test_shared_with_me_is_true_for_a_named_share() -> None:
    # A colleague's job order shared to me: shared_with_me is True.
    ...


async def test_shared_with_me_is_true_for_a_tenant_broadcast() -> None:
    # A broadcast reaches me the same way a named share does.
    ...


async def test_shared_with_me_is_true_even_when_i_also_own_it() -> None:
    """The flag means "a share reaches you", not "only a share reaches you".

    A flag that flipped to false the moment you also owned the row would be
    describing something else.
    """
    ...
```

Write all five out fully, following the seeding style in `tests/test_opportunity_visibility_routes.py`.

- [ ] **Step 2: Run and watch them fail**

```bash
uv run pytest tests/test_opportunity_payload_fields.py -v
```

Expected: FAIL — `KeyError: 'assigned_user_id'`.

- [ ] **Step 3: Add the fields**

`_payload` currently takes `(row, internet_message_id, graph_message_id, evidence, codes)`. Add two parameters — `assignee_name: str | None` and `shared_with_me: bool` — and five keys:

```python
        "assigned_user_id": str(row.assigned_user_id) if row.assigned_user_id else None,
        # Denormalised rather than resolved in the browser against the members
        # list: otherwise every row's rendering depends on a second request
        # having already landed, which is a race the list does not otherwise
        # have.
        "assignee_name": assignee_name,
        "client_id": str(row.client_id) if row.client_id else None,
        "source": row.source,
        # "A share is one of the reasons you can see this", not "the only
        # reason" — see the design note on why the two differ.
        "shared_with_me": shared_with_me,
```

In `list_opportunities`, resolve both in the page query rather than per row:

- `assignee_name` — LEFT JOIN `users` on `(tenant_id, assigned_user_id)` and select `func.coalesce(User.preferred_name, User.display_name, User.email)`. `preferred_name` first because `app/models/tenant.py` says it takes priority wherever a name is shown.
- `shared_with_me` — an `EXISTS` correlated on `Opportunity.id` against `opportunity_shares`, matching `scope='tenant'` OR (`scope='user'` AND `shared_with_user_id = :caller`), added as a labelled column.

Both go in the existing page SELECT. Do not add a second round trip and do not loop in Python over the page.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_opportunity_payload_fields.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Run the full backend suite and lint**

```bash
uv run pytest -q && uv run ruff check .
```

Expected: all pass (baseline 1573). `test_opportunity_routes_guarded.py` must still pass — you have not added a route.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/opportunities.py backend/tests/test_opportunity_payload_fields.py
git commit -m "Say who owns a job order when listing it"
```

---

### Task 2: A members endpoint

**Files:**
- Create: `backend/app/api/members.py`
- Modify: `backend/app/main.py` (register the router)
- Test: `backend/tests/test_members_api.py` (create)

**Interfaces:**
- Produces: `GET /api/members` → `[{"id": str, "name": str, "email": str, "role": str}]`, sorted by name. Tasks 4, 5, 9 and 10 consume it.

- [ ] **Step 1: Write the failing test**

```python
"""The agency's own staff list, for pickers that name a colleague."""


async def test_it_lists_everyone_in_my_agency() -> None:
    ...


async def test_it_never_lists_another_agency() -> None:
    """Two tenants, each with users; A must not see B's."""
    ...


async def test_preferred_name_wins_over_display_name() -> None:
    """A picker that ignored preferred_name would call someone by a name they
    had explicitly replaced."""
    ...


async def test_a_user_with_no_names_falls_back_to_the_email_local_part() -> None:
    """So the picker never renders a blank row."""
    # display_name and preferred_name both NULL, email "raj@agency.sg" -> "raj"
    ...


async def test_it_reports_the_owner_role() -> None:
    ...
```

- [ ] **Step 2: Run and watch it fail**

```bash
uv run pytest tests/test_members_api.py -v
```

Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Write the module**

```python
"""Who is in this agency.

Not paginated: the vertical is agencies of 3-50 recruiters, and a picker that
pages is a picker that hides the person you want.

Any authenticated member may call it — an agency's own staff list is not a
secret from its own staff. RLS scopes the read to the caller's tenant, the
same as every other read in this codebase.
"""

router = APIRouter(tags=["members"])


@router.get("/members")
async def list_members(request: Request) -> list[dict]:
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        rows = (
            await session.execute(
                select(User.id, User.preferred_name, User.display_name, User.email, User.role)
            )
        ).all()

    return sorted(
        (
            {
                "id": str(row.id),
                # preferred_name first: `app/models/tenant.py` says it takes
                # priority everywhere a name is shown. The email local-part is
                # the last resort so no row renders blank.
                "name": (
                    (row.preferred_name or "").strip()
                    or (row.display_name or "").strip()
                    or row.email.split("@")[0]
                ),
                "email": row.email,
                "role": row.role,
            }
            for row in rows
        ),
        key=lambda m: m["name"].casefold(),
    )
```

Use the plain synchronous `_require_session` — this route does not need the caller's role. Register the router in `app/main.py` beside the others.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_members_api.py tests/test_routing.py -v
```

Expected: 5 passed plus routing green.

- [ ] **Step 5: Full suite, lint, commit**

```bash
uv run pytest -q && uv run ruff check .
git add backend/app/api/members.py backend/app/main.py backend/tests/test_members_api.py
git commit -m "Name the people a job order can be handed to"
```

---

### Task 3: The `?scope=` filter

**Files:**
- Modify: `backend/app/api/opportunities.py` — `list_opportunities` signature, page query and counts
- Test: `backend/tests/test_opportunity_scope_filter.py` (create)

**Interfaces:**
- Consumes: Task 1's payload fields.
- Produces: `GET /api/opportunities?scope=mine|queue|shared_with_me|all`, default `all`.

- [ ] **Step 1: Write the failing test**

```python
"""Ownership is a second axis, independent of review status."""


async def test_scope_mine_returns_only_my_job_orders() -> None: ...
async def test_scope_queue_returns_only_unassigned_ones() -> None: ...
async def test_scope_shared_with_me_returns_shares_and_broadcasts() -> None: ...
async def test_scope_all_is_the_default_and_matches_the_predicate() -> None:
    """`all` means everything the caller may see, which is not everything the
    agency has."""
    ...


async def test_no_scope_can_widen_visibility() -> None:
    """Each scope is a filter WITHIN what the predicate allows. A colleague's
    private job order appears under none of the four."""
    ...


async def test_the_counts_follow_the_scope() -> None:
    """A count that ignored the scope would say twelve and then show four."""
    ...


async def test_an_unknown_scope_is_refused() -> None:
    # 422 from the Literal, not a silent fallback to `all`.
    ...
```

- [ ] **Step 2: Run and watch it fail**

```bash
uv run pytest tests/test_opportunity_scope_filter.py -v
```

Expected: FAIL — the parameter is ignored, so `scope=queue` returns everything.

- [ ] **Step 3: Implement it**

Add to the signature, beside the existing `status: StatusFilter | None = None`:

```python
    scope: ScopeFilter = "all",
```

with `ScopeFilter = Literal["mine", "queue", "shared_with_me", "all"]` declared beside `StatusFilter`. Then build the clause:

```python
def _scope_clause(scope: str, user_id: uuid.UUID):
    """A filter WITHIN what `visible_opportunities` already allows, never a
    widening of it. It is ANDed with the predicate, never substituted for it.
    """
    if scope == "mine":
        return Opportunity.assigned_user_id == user_id
    if scope == "queue":
        return Opportunity.assigned_user_id.is_(None)
    if scope == "shared_with_me":
        return _shared_with_me_exists(user_id)
    return true_()
```

`_shared_with_me_exists(user_id)` is the same expression Task 1 added for the payload column — extract it to one helper and call it from both, so the chip and the row badge can never disagree.

Apply the clause to **both** the counts query and the page query, alongside `visible`.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_opportunity_scope_filter.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Full suite, lint, size check**

```bash
uv run pytest -q && uv run ruff check . && wc -l app/api/opportunities.py
```

Expected: green; the file is under 1500.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/opportunities.py backend/tests/test_opportunity_scope_filter.py
git commit -m "Let a recruiter ask for their own work, or the queue"
```

---

## Phase B — frontend foundations

### Task 4: The person primitive and the members hook

**Files:**
- Create: `frontend/app/dashboard/person.tsx`, `frontend/app/dashboard/person.test.tsx`
- Create: `frontend/app/dashboard/members.ts`, `frontend/app/dashboard/members.test.ts`
- Modify: `frontend/app/api.ts` (add `MEMBERS_PATH`)
- Modify: `frontend/app/dashboard/clients/client-logo.tsx` (import the shared helpers instead of its own copies)
- Modify: `frontend/app/globals.css` or `app.css` (add `.person-initials`)

**Interfaces:**
- Produces:
  - `initialsFor(name: string): string` and `colorFor(seed: string): string`, exported from `person.tsx`
  - `<Initials name={string} seed={string} size?: number />`
  - `type Member = { id: string; name: string; email: string; role: string }`
  - `useMembers(): { status: "loading" | "ready" | "unreadable"; members: Member[]; message?: string }`
- Tasks 5, 7, 8, 9, 10 consume these.

- [ ] **Step 1: Write the failing tests**

`person.test.tsx`:

```tsx
describe("initialsFor", () => {
  it("takes the first and last initial of a full name", () => {
    expect(initialsFor("Priya Nair")).toBe("PN");
  });
  it("takes two letters of a single name", () => {
    expect(initialsFor("Priya")).toBe("PR");
  });
  it("gives a question mark for an empty name", () => {
    expect(initialsFor("   ")).toBe("?");
  });
});

describe("colorFor", () => {
  it("is deterministic", () => {
    expect(colorFor("abc")).toBe(colorFor("abc"));
  });
  it("keys on the seed, not the name, so renaming someone keeps their colour", () => {
    // The whole reason the seed is separate from the name.
    const id = "0f8f-user-id";
    expect(colorFor(id)).toBe(colorFor(id));
  });
});

describe("Initials", () => {
  it("renders the initials with an accessible name", () => {
    render(<Initials name="Priya Nair" seed="user-1" />);
    expect(screen.getByRole("img", { name: "Priya Nair" })).toHaveTextContent("PN");
  });
});
```

`members.test.ts`:

```tsx
it("fetches once across two mounts", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify([{ id: "1", name: "Priya Nair", email: "p@a.sg", role: "recruiter" }]), { status: 200 }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const first = renderHook(() => useMembers());
  await waitFor(() => expect(first.result.current.status).toBe("ready"));
  first.unmount();
  const second = renderHook(() => useMembers());
  await waitFor(() => expect(second.result.current.status).toBe("ready"));
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("reports unreadable when the request fails", async () => { ... });
```

- [ ] **Step 2: Run and watch them fail**

```bash
npx vitest run app/dashboard/person.test.tsx app/dashboard/members.test.ts
```

Expected: FAIL — modules do not exist.

- [ ] **Step 3: Write `person.tsx`**

Move `LOGO_COLORS`, `colorFor` and `initialsFor` out of `client-logo.tsx:38-54` and into `person.tsx`, **changing `colorFor`'s parameter name from `name` to `seed`** and nothing else about its body:

```tsx
/** Deterministic, not random: the same seed always lands on the same colour.
 *
 * The seed is separate from the name on purpose. A person's colour keys on
 * their user id, so fixing a typo in their name does not recolour them
 * everywhere. A client logo keys on the client's name, which is what it
 * already did — passing the name preserves every existing logo's colour.
 */
export function colorFor(seed: string): string { ... }

export function initialsFor(name: string): string { ... }

export function Initials({ name, seed, size = 24 }: {
  name: string;
  seed: string;
  size?: number;
}) {
  return (
    <span
      className="person-initials"
      role="img"
      aria-label={name}
      style={{ width: size, height: size, background: colorFor(seed), fontSize: size * 0.4 }}
    >
      {initialsFor(name)}
    </span>
  );
}
```

Then update `client-logo.tsx` to import `colorFor` and `initialsFor` from `../person` and delete its local copies. **It must keep passing the client's name as the seed** — passing the id instead would change the colour of every client logo in the product.

CSS, matching the existing `.ca-initials` at `app.css:663-673`:

```css
.person-initials {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  color: #fff;
  font-weight: 600;
  line-height: 1;
  vertical-align: middle;
}
```

- [ ] **Step 4: Write `members.ts`**

```tsx
/** The agency's staff list.
 *
 * Cached at module scope rather than refetched per mount: the list changes
 * when somebody joins the agency, which is not on the timescale of a dialog
 * opening. `resetMembers()` exists for sign-out and for tests.
 */
let cache: Promise<Member[]> | null = null;

export function resetMembers(): void { cache = null; }

export function useMembers(): MembersState { ... }
```

Add `export const MEMBERS_PATH = `${API_BASE}/api/members`;` to `frontend/app/api.ts`, following the file's existing convention.

Call `resetMembers()` wherever sign-out clears other state.

- [ ] **Step 5: Run the tests**

```bash
npx vitest run app/dashboard/person.test.tsx app/dashboard/members.test.ts
```

Expected: all pass.

- [ ] **Step 6: Confirm the extraction broke nothing**

```bash
npm test
```

Expected: green, including the existing client-logo tests. If a client-logo test asserts a specific colour, it must still pass — that is the check that the seed change did not alter existing logos.

- [ ] **Step 7: Commit**

```bash
git add frontend/app/dashboard/person.tsx frontend/app/dashboard/person.test.tsx \
        frontend/app/dashboard/members.ts frontend/app/dashboard/members.test.ts \
        frontend/app/dashboard/clients/client-logo.tsx frontend/app/api.ts frontend/app/app.css
git commit -m "Give a person a face, and stop writing initials twice"
```

---

### Task 5: The colleague picker

**Files:**
- Create: `frontend/app/dashboard/member-picker.tsx`, `frontend/app/dashboard/member-picker.test.tsx`
- Modify: `frontend/app/app.css`

**Interfaces:**
- Consumes: `useMembers()`, `Member`, `<Initials>` (Task 4); `useAuth()` and `displayNameOf` from `frontend/app/auth.ts`.
- Produces:

```tsx
export function MemberPicker({ selected, onChange, exclude, label }: {
  selected: string[];
  onChange: (ids: string[]) => void;
  exclude?: string[];
  label: string;
}): JSX.Element

export function MemberSelect({ value, onChange, allowNone, label }: {
  value: string | null;
  onChange: (id: string | null) => void;
  allowNone?: boolean;
  label: string;
}): JSX.Element
```

`MemberPicker` is multi-select (sharing). `MemberSelect` is single-select (assigning a job order or a client). Tasks 8, 9 and 10 consume them.

- [ ] **Step 1: Write the failing test**

```tsx
it("excludes the signed-in user", async () => {
  // Sharing with yourself is a no-op the API silently skips; offering it
  // invites the confusion.
  ...
  expect(screen.queryByText("Mei Wong")).not.toBeInTheDocument();
});

it("filters the list as you type", async () => { ... });

it("adds a colleague as a chip and reports the id", async () => { ... });

it("removes a chip", async () => { ... });

it("excludes anyone named in `exclude`", async () => {
  // Used to hide people a job order is already shared with.
  ...
});

it("shows the owner role beside the name", async () => { ... });

it("says so when the list cannot be read", async () => { ... });
```

- [ ] **Step 2: Run and watch it fail**

```bash
npx vitest run app/dashboard/member-picker.test.tsx
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

A text input filtering `useMembers()`, chips for the selected, keyboard-navigable list. Exclude the caller by reading `useAuth()` — do not accept the caller's id as a prop, as every call site would then have to remember to pass it.

- [ ] **Step 4: Tests, full suite, commit**

```bash
npx vitest run app/dashboard/member-picker.test.tsx && npm test
git add frontend/app/dashboard/member-picker.tsx frontend/app/dashboard/member-picker.test.tsx frontend/app/app.css
git commit -m "Let a recruiter name a colleague"
```

---

## Phase C — job orders

### Task 6: Scope, claim and assign in the data layer

**Files:**
- Modify: `frontend/app/dashboard/opportunities.ts` (557 LOC — watch it)
- Modify: `frontend/app/api.ts`
- Test: `frontend/app/dashboard/opportunities.test.ts` (create or extend)

**Interfaces:**
- Consumes: Tasks 1 and 3.
- Produces:
  - `Opportunity` gains `assigned_user_id`, `assignee_name`, `client_id`, `source`, `shared_with_me`
  - `type Scope = "mine" | "queue" | "shared_with_me" | "all"`
  - `useOpportunities()` returns `scope` and `setScope`
  - `claimOpportunity(id): Promise<{ ok: true } | { ok: false; conflict: boolean; message: string }>`
  - `assignOpportunity(id, userId: string | null): Promise<...>`

- [ ] **Step 1: Write the failing test**

```tsx
it("puts the scope in the query string", async () => {
  // listUrl already carries limit/offset/sort/descending/status/q.
  expect(fetchMock.mock.calls[0][0]).toContain("scope=queue");
});

it("reports a 409 claim as a conflict, not a generic failure", async () => {
  // "Someone else has taken this one" is a different sentence from "something
  // went wrong", and the difference is the whole point.
  const result = await claimOpportunity("abc");
  expect(result).toEqual({ ok: false, conflict: true, message: "Someone else has taken this one." });
});

it("reports a 404 as no-longer-available", async () => { ... });
it("assign with null releases the job order", async () => { ... });
```

- [ ] **Step 2: Run and watch it fail**

```bash
npx vitest run app/dashboard/opportunities.test.ts
```

- [ ] **Step 3: Implement**

Extend `listUrl` (currently at `opportunities.ts:130-140`) with one line, keeping its shape:

```tsx
  if (scope !== "all") params.set("scope", scope);
```

Add `scope`/`setScope` to the hook's state beside `filter`/`setFilter`, and include `scope` in the effect's dependency list so changing it refetches. Reset `offset` to 0 when the scope changes — otherwise switching to a short list lands on an empty page.

Add path helpers to `api.ts` following the `{entity}{Action}Path(id)` convention:

```tsx
export const opportunityClaimPath = (id: string) => `${API_BASE}/api/opportunities/${encodeURIComponent(id)}/claim`;
export const opportunityAssignPath = (id: string) => `${API_BASE}/api/opportunities/${encodeURIComponent(id)}/assign`;
export const opportunitySharesPath = (id: string) => `${API_BASE}/api/opportunities/${encodeURIComponent(id)}/shares`;
export const opportunitySharePath = (id: string, shareId: string) =>
  `${API_BASE}/api/opportunities/${encodeURIComponent(id)}/shares/${encodeURIComponent(shareId)}`;
```

The mutations map status to a message rather than throwing, so callers render a sentence:

```tsx
const CLAIM_MESSAGES: Record<number, string> = {
  409: "Someone else has taken this one.",
  404: "This job order is no longer available.",
};
```

- [ ] **Step 4: Tests, then size check**

```bash
npx vitest run app/dashboard/opportunities.test.ts && wc -l app/dashboard/opportunities.ts
```

If it exceeds ~700 LOC, split the mutations into `frontend/app/dashboard/opportunity-actions.ts` before committing and say so in the report.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/dashboard/opportunities.ts frontend/app/api.ts frontend/app/dashboard/opportunities.test.ts
git commit -m "Ask the API for one recruiter's work"
```

---

### Task 7: Scope chips and the assignee avatar

**Files:**
- Modify: `frontend/app/dashboard/job-orders.tsx` (345 LOC)
- Modify: `frontend/app/dashboard/job-orders-table.tsx` (235 LOC)
- Modify: `frontend/app/app.css`
- Test: `frontend/app/dashboard/job-orders-scope.test.tsx` (create)

**Interfaces:**
- Consumes: Tasks 4 and 6.

- [ ] **Step 1: Write the failing test**

```tsx
it("renders a second chip row that combines with the status chips", async () => {
  // Ownership and review state are independent axes. A recruiter wanting
  // "mine, needing review" should not have to choose which question to ask.
  ...
  expect(lastUrl(fetchMock)).toContain("scope=mine");
  expect(lastUrl(fetchMock)).toContain("status=needs_review");
});

it("shows initials in the company cell for an assigned job order", async () => { ... });

it("shows a dashed empty circle for a queue item", async () => { ... });

it("puts the assignee name in the title so a hover names them", async () => { ... });

it("does not add a ninth column", async () => {
  // The table is table-layout:fixed and three recent commits went into
  // fitting eight columns to their content.
  expect(screen.getAllByRole("columnheader")).toHaveLength(8);
});
```

- [ ] **Step 2: Run and watch it fail**

```bash
npx vitest run app/dashboard/job-orders-scope.test.tsx
```

- [ ] **Step 3: Add the chip row**

In `job-orders.tsx`, add a second row beside the existing one at lines 142-157, reusing `.jo-chip` exactly:

```tsx
const SCOPES: { key: Scope; label: string }[] = [
  { key: "mine", label: "Mine" },
  { key: "queue", label: "Queue" },
  { key: "shared_with_me", label: "Shared with me" },
  { key: "all", label: "All" },
];
```

No count badge on these — the API returns counts per review status, not per scope, and a chip showing a stale or invented number is worse than one showing none.

- [ ] **Step 4: Add the avatar to the company cell**

In `job-orders-table.tsx`, inside the existing company `<td>` (lines 119-135), before `<Value text={row.company_name_raw} />`:

```tsx
<span
  className="jo-owner"
  title={row.assignee_name ?? "Unassigned"}
>
  {row.assigned_user_id ? (
    <Initials name={row.assignee_name ?? "?"} seed={row.assigned_user_id} size={18} />
  ) : (
    <span className="jo-owner-empty" role="img" aria-label="Unassigned" />
  )}
</span>
```

Leave the existing `aria-label` on the button alone — it already names the row, and the avatar's own label covers the owner.

CSS:

```css
.jo-owner { margin-right: 6px; }
.jo-owner-empty {
  display: inline-block;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1px dashed var(--ink-300);
  vertical-align: middle;
}
```

- [ ] **Step 5: Tests and the whole suite**

```bash
npx vitest run app/dashboard/job-orders-scope.test.tsx && npm test
```

- [ ] **Step 6: Commit**

```bash
git add frontend/app/dashboard/job-orders.tsx frontend/app/dashboard/job-orders-table.tsx \
        frontend/app/dashboard/job-orders-scope.test.tsx frontend/app/app.css
git commit -m "Show whose job order it is without spending a column"
```

---

### Task 8: Owner, claim and assign in the detail panel

**Files:**
- Modify: `frontend/app/dashboard/detail-panel.tsx` (216 LOC)
- Modify: `frontend/app/dashboard/job-orders.tsx` (wire the callbacks)
- Test: `frontend/app/dashboard/detail-panel-ownership.test.tsx` (create)

**Interfaces:**
- Consumes: Tasks 4, 5, 6. `DetailPanel` gains `onClaim`, `onAssign` and `onVanished` props beside the existing `row` and `onReview`.

- [ ] **Step 1: Write the failing test**

```tsx
it("names the owner", async () => { ... });
it("says Unassigned for a queue item", async () => { ... });

it("offers Claim only when unassigned", async () => { ... });

it("shows the 409 sentence when someone else claims first", async () => {
  expect(await screen.findByRole("alert")).toHaveTextContent("Someone else has taken this one.");
});

it("clears itself and reports upward on a 404", async () => {
  // A share was withdrawn under the open panel. Sitting there showing stale
  // fields with a red message beside them is what a generic error handler
  // does; blanking silently reads as a bug.
  expect(onVanished).toHaveBeenCalled();
  expect(screen.queryByText("Care assistant")).not.toBeInTheDocument();
});

it("says to claim it first when a 403 comes back on an unassigned job order", async () => {
  // "Shared with you, not assigned to you" would be a lie here — nobody was
  // assigned it.
  expect(await screen.findByRole("alert")).toHaveTextContent("Claim this job order before editing it.");
});

it("says shared-not-assigned on a 403 for an assigned one", async () => { ... });

it("offers Assign to the owner role and to the assignee, and to nobody else", async () => { ... });
```

- [ ] **Step 2: Run and watch it fail**

```bash
npx vitest run app/dashboard/detail-panel-ownership.test.tsx
```

- [ ] **Step 3: Implement**

Add an owner line to the header block (around lines 82-96), and the buttons beside the existing review toggle at lines 168-182, reusing `.btn`/`.btn-secondary`.

The 403 message depends on the row, not only on the status:

```tsx
function forbiddenMessage(row: Opportunity): string {
  return row.assigned_user_id
    ? "This job order is shared with you, not assigned to you."
    : "Claim this job order before editing it.";
}
```

- [ ] **Step 4: Tests, whole suite, commit**

```bash
npx vitest run app/dashboard/detail-panel-ownership.test.tsx && npm test
git add frontend/app/dashboard/detail-panel.tsx frontend/app/dashboard/job-orders.tsx frontend/app/dashboard/detail-panel-ownership.test.tsx
git commit -m "Let a recruiter take a job order off the queue"
```

---

## Phase D — sharing

### Task 9: The share dialog

**Files:**
- Create: `frontend/app/dashboard/shares.ts`, `frontend/app/dashboard/share-dialog.tsx`, `frontend/app/dashboard/share-dialog.test.tsx`
- Modify: `frontend/app/dashboard/detail-panel.tsx` (the Share button)
- Modify: `frontend/app/app.css`

**Interfaces:**
- Consumes: Tasks 4, 5, 6, 8.
- Produces: `listShares(id)`, `shareOpportunity(id, {scope, user_ids, note})`, `unshare(id, shareId)`; `<ShareDialog row onClose />`.

- [ ] **Step 1: Write the failing test**

```tsx
it("posts the picked colleagues", async () => {
  expect(lastBody(fetchMock)).toEqual({ scope: "user", user_ids: ["u2"], note: null });
});

it("posts a tenant broadcast when the checkbox is ticked", async () => {
  expect(lastBody(fetchMock)).toEqual({ scope: "tenant", user_ids: [], note: null });
});

it("disables broadcast for a share recipient and says why", async () => {
  // A recipient may pass a job order to a named colleague but not throw
  // someone else's client work open to the office.
  expect(screen.getByLabelText(/whole agency/i)).toBeDisabled();
  expect(screen.getByText(/only the assigned recruiter/i)).toBeInTheDocument();
});

it("disables broadcast on an unassigned job order for a non-owner", async () => {
  // The API gates on can_edit, which refuses unassigned rows, falling back to
  // the owner role.
  expect(screen.getByText(/claim it first/i)).toBeInTheDocument();
});

it("allows an owner-role user to broadcast an unassigned job order", async () => { ... });

it("lists who it is already shared with, and removes one", async () => { ... });

it("says removal revokes sight, not their work", async () => { ... });

it("hides people it is already shared with from the picker", async () => { ... });

it("sends no note field when the note is blank", async () => { ... });
```

- [ ] **Step 2: Run and watch it fail**

```bash
npx vitest run app/dashboard/share-dialog.test.tsx
```

- [ ] **Step 3: Implement**

Build on the existing `Dialog` at `frontend/app/dashboard/dialog.tsx` — do not write a new modal. The broadcast gate:

```tsx
const iMayBroadcast =
  me.user.role === "owner" || (row.assigned_user_id !== null && row.assigned_user_id === me.user.id);
```

Disabled rather than hidden, with the reason shown: a control that vanishes teaches nothing, and this rule is worth understanding.

- [ ] **Step 4: Tests, whole suite, commit**

```bash
npx vitest run app/dashboard/share-dialog.test.tsx && npm test
git add frontend/app/dashboard/shares.ts frontend/app/dashboard/share-dialog.tsx \
        frontend/app/dashboard/share-dialog.test.tsx frontend/app/dashboard/detail-panel.tsx frontend/app/app.css
git commit -m "Let a job order travel to whoever can fill it"
```

---

## Phase E — clients and manual creation

### Task 10: The client's recruiter

**Files:**
- Modify: `frontend/app/dashboard/clients/clients.ts`
- Modify: `frontend/app/dashboard/clients/client-panel.tsx` (698 LOC — if this task pushes it past ~900, split the assignment control into `client-assignee.tsx` and say so)
- Modify: `frontend/app/api.ts`
- Test: `frontend/app/dashboard/clients/client-assignee.test.tsx` (create)

**Interfaces:**
- Consumes: Tasks 4 and 5.
- Produces: `Client` gains `assigned_user_id` and `assignee_name`; `setClientAssignee(id, userId, moveOpportunities)`, `addCollaborator`, `removeCollaborator`.

- [ ] **Step 1: Write the failing test**

```tsx
it("defaults the move checkbox to on", async () => {
  // A client changing hands normally means the work changes hands.
  expect(screen.getByLabelText(/move this client's job orders/i)).toBeChecked();
});

it("reports how many job orders moved", async () => {
  // A reassignment that moves a dozen job orders silently is what the count
  // exists to prevent.
  expect(await screen.findByText(/12 job orders moved to Sarah/i)).toBeInTheDocument();
});

it("sends move_open_opportunities false when unticked", async () => { ... });

it("is hidden when the caller is neither the owner nor the assignee", async () => {
  // Mirrors the 403 the API returns — the interface should not offer an
  // action that will be refused.
  ...
});

it("is offered on an unassigned client to any recruiter", async () => { ... });

it("says collaborators grant no access to the client's job orders", async () => {
  // Without that said plainly, the list reads like a share.
  ...
});

it("adding the same collaborator twice does not error", async () => { ... });
```

- [ ] **Step 2: Run, watch fail, implement, verify**

```bash
npx vitest run app/dashboard/clients/client-assignee.test.tsx
```

Reuse `MemberSelect` from Task 5. The request field is `move_open_opportunities` and the response key is `opportunities_moved` — the names differ and both are fixed by the shipped API.

- [ ] **Step 3: Whole suite and commit**

```bash
npm test && wc -l app/dashboard/clients/client-panel.tsx
git add frontend/app/dashboard/clients/ frontend/app/api.ts
git commit -m "Say which recruiter looks after which client"
```

---

### Task 11: Typing in a job order

**Files:**
- Create: `frontend/app/dashboard/job-order-form.tsx`, `frontend/app/dashboard/job-order-form.test.tsx`
- Modify: `frontend/app/dashboard/job-orders.tsx` (the New job order button)
- Modify: `frontend/app/dashboard/opportunities.ts` (`createOpportunity`)

**Interfaces:**
- Consumes: Tasks 5, 6, 7.

- [ ] **Step 1: Write the failing test**

```tsx
it("posts a manual job order", async () => {
  expect(lastBody(fetchMock)).toMatchObject({ job_title_raw: "Warehouse assistant" });
});

it("lands assigned to its creator", async () => {
  // You typed it in, it is yours - the API does this; the test pins that the
  // form does not try to set an assignee itself.
  expect(lastBody(fetchMock)).not.toHaveProperty("assigned_user_id");
});

it("allows an empty client", async () => {
  // A job order taken over the phone from a company you have not recorded yet
  // has no client, and client_id is nullable precisely for that.
  expect(lastBody(fetchMock).client_id).toBeNull();
});

it("searches clients as you type rather than preloading them", async () => {
  // Clients are paginated and an agency accumulates hundreds; members are
  // 3-50 and load once. The two pickers are not the same component.
  await userEvent.type(screen.getByLabelText("Client"), "sun");
  await waitFor(() => expect(lastUrl(fetchMock)).toContain("q=sun"));
});

it("shows the new job order in the list without a reload", async () => { ... });
```

- [ ] **Step 2: Run, watch fail, implement**

```bash
npx vitest run app/dashboard/job-order-form.test.tsx
```

The client field is a type-to-search over `GET /api/clients?q=`, not `MemberPicker` and not a preloaded dropdown.

- [ ] **Step 3: Whole suite, both suites, commit**

```bash
npm test
cd ../backend && uv run pytest -q && uv run ruff check .
git add frontend/app/dashboard/
git commit -m "Let a recruiter type in a job order taken over the phone"
```

---

## Deployment note

The three backend changes deploy with the frontend in one image — `api` serves the Next.js static export. **No migration:** every column this plan reads already exists in production as of 2026-07-31. Merging to `main` triggers CI/CD, which runs `alembic upgrade head` (a no-op here) and redeploys.

## Self-review

**Spec coverage.** Payload fields → Task 1. Members endpoint → Task 2. `?scope=` → Task 3. Person primitive and `useMembers` → Task 4. Picker → Task 5. Job order data layer → Task 6. Chips and avatar → Task 7. Owner/claim/assign → Task 8. Sharing → Task 9. Client assignee and collaborators → Task 10. Manual creation → Task 11. Error copy → Tasks 6 and 8. Panel-vanishes → Task 8.

**Two things found while writing:**

1. `colorFor` in `client-logo.tsx` hashes the **name**. The spec wants a person's colour keyed to their **id** so a rename does not recolour them — but keying client logos on the id would change the colour of every existing logo. Task 4 therefore renames the parameter to `seed` and has each caller choose: id for people, name for clients.
2. The scope chips carry no count badge. The API returns counts per review status, not per scope, and inventing one client-side would be a number that disagrees with the server on exactly the rows where it matters.

**Not covered, and deliberately:** cross-user live updates for assign/share — `useLive` has no nudge kind for them, which the spec names as follow-up work.
