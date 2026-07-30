# Job order assignment and sharing — the screens

Decided 2026-07-31. The user-facing half of
[the assignment and sharing design](2026-07-30-job-order-assignment-and-sharing-design.md),
which shipped as backend only on 2026-07-30.

Everything that design describes is live in production and invisible. A job
order has an assigned recruiter, can be shared with a named colleague or the
whole agency, and sits in a claimable queue when nobody owns it — and none of
that appears anywhere in the interface. This spec is the screens.

It also closes two gaps in the API that the backend work missed, because that
work was designed around operations rather than around the screens that would
drive them.

## The three API gaps

**The opportunity payload returns none of the new columns.** `_payload` in
`app/api/opportunities.py` returns `id`, `company_name_raw`, `quality_state`,
`review_status` and the rest, but not `assigned_user_id`, `client_id` or
`source`. A list that cannot say who owns a row cannot show ownership, and
cannot tell a queue item from an assigned one.

**Nothing lists the people in an agency.** Sharing means naming a colleague.
There is no endpoint returning the tenant's users, so the picker has nothing to
pick from.

**`GET /api/opportunities` has no `?scope=` parameter.** The 2026-07-30 plan
specified one — `mine | queue | shared_with_me | all` — and it was never built.
`list_opportunities` still takes only `limit`, `offset`, `status`, `q`, `sort`
and `descending`. The omission survived because that task's other three routes
(claim, assign, manual create) were built and reviewed, and nothing checked the
one item that was a query parameter rather than a route.

All three are additive and small. They are in this spec rather than their own
because the UI is untestable without them.

### `_payload` gains five fields

| Field | Why |
|---|---|
| `assigned_user_id` | who owns it; NULL is the queue |
| `assignee_name` | denormalised, so a row renders from one response rather than a join in the browser |
| `client_id` | lets the list link a job order to its client |
| `source` | `pipeline` or `manual` — a hand-typed job order should be identifiable as one |
| `shared_with_me` | true when a share row grants the caller sight of this job order |

`assignee_name` resolves `preferred_name` → `display_name` → the email
**local-part**, the identical chain `GET /api/members` and the clients screen
use. The local part and not the whole address: a colleague with no name set
must read as "raj" everywhere, and "raj" in the sharing picker beside
"raj@agency.sg" in the list looks like two different people.

`assignee_name` is denormalised deliberately. The alternative — return only the
id and have the browser resolve it against the members list — makes every row's
rendering depend on a second request having already landed, which is a race the
list does not otherwise have.

`shared_with_me` is a boolean expression in the same SELECT — an `EXISTS`
against `opportunity_shares` for this caller, alongside the visibility clause
rather than after it. Not a second round trip, and not a post-hoc loop in
Python over the page.

It means **"a share is one of the reasons you can see this"**, not "the only
reason". A job order that is both assigned to you and broadcast to the agency
returns `true`, and that is correct: the two facts are independent, and a flag
that flipped to false the moment you also owned the row would be describing
something else. A `scope='tenant'` broadcast counts, the same as a share naming
you — the UI marks the row as reaching you through sharing either way.

The `?scope=shared_with_me` filter uses this same expression, so the chip and
the row badge can never disagree.

### `?scope=` on the opportunity list

`mine | queue | shared_with_me | all`, defaulting to `all`.

Each is a filter **within** what the visibility predicate already allows, never
a widening of it: `all` means everything the caller may see, which in an agency
of eight recruiters is not everything the agency has. `queue` is
`assigned_user_id IS NULL`; `mine` is `assigned_user_id = :caller`;
`shared_with_me` is an `EXISTS` against `opportunity_shares`.

The tab counts must be computed under the same scope as the page, the way they
already are under the visibility predicate — a count that ignores the scope
tells a recruiter there are twelve and then shows them four.

### `GET /api/members`

Returns every user in the caller's tenant:

```json
[{"id": "...", "name": "Priya Nair", "email": "priya@agency.sg", "role": "recruiter"}]
```

Not paginated — the vertical is agencies of 3–50 recruiters, and a picker that
pages is a picker that hides the person you want.

`name` resolves `preferred_name` → `display_name` → the email local-part.
`preferred_name` first because `app/models/tenant.py` says it is the user's own
choice of name and takes priority wherever a name is shown, and a picker that
ignored it would call someone by a name they had explicitly replaced. The
local-part fallback means the picker never renders a blank row. The whole chain
lives in the API, not the browser, so every consumer gets the same answer.

`email` is included because it disambiguates. Two people called Sarah is not a
hypothetical in a small office, and sharing a client's job order with the wrong
one is the failure this field prevents. Role is included so the interface can
mark the owner.

Tenant-scoped by RLS like every other read. Any authenticated member may call
it: the agency's own staff list is not a secret from its own staff.

## Frontend architecture

The existing frontend is plain React hooks — no state library, no component
library, no Tailwind. Per-domain modules (`opportunities.ts`, `clients.ts`,
`candidates.ts`) each own their fetching; styling is hand-rolled CSS with design
tokens in `globals.css`. This spec adds nothing to `package.json`.

| File | Responsibility |
|---|---|
| `dashboard/members.ts` | `useMembers()` — fetches once and caches in module scope |
| — | **caller identity: nothing new.** `GET /api/auth/me` already returns `id`, `email`, `display_name`, `preferred_name` and `role`, and `app/auth.ts` already exposes it as `useAuth()`. Every "is this me?" and "am I an owner?" decision below reads that, not a new endpoint |
| `dashboard/person.tsx` | `<Initials user>` — the avatar primitive |
| `dashboard/member-picker.tsx` | Chips plus a filtered list of colleagues |
| `dashboard/share-dialog.tsx` | The share dialog, on the existing `Dialog` |
| `dashboard/job-order-form.tsx` | Manual creation |
| `dashboard/opportunities.ts` | *extended* — `scope`, `claim()`, `assign()` |
| `dashboard/clients/clients.ts` | *extended* — assignee and collaborators |
| `dashboard/clients/client-panel.tsx` | *extended* — the assignee control |

`member-picker.tsx` has three callers — sharing, assigning a job order, and
assigning a client. That shared primitive is the reason this is one spec rather
than four; designed apart, it would have been built three times.

`useMembers` caches at module scope rather than refetching per mount. The staff
list changes when somebody joins the agency, which is not on the timescale of a
dialog opening. The cache is dropped on sign-out along with everything else.

`<Initials>` derives its colour from a hash of the user id, so one person is the
same colour everywhere they appear. Deriving from the *name* would recolour
someone when they fix a typo in it.

## The screens

### Job orders

The list gains a second row of filter chips — `Mine · Queue · Shared with me ·
All` — mapping to the `?scope=` parameter added above. It sits
beside the existing review-status chips and combines with them: ownership and
review state are independent axes, and a recruiter wanting "mine, needing
review" should not have to choose which question to ask.

`All` means everything the caller may see, which is not everything in the
agency. That is the visibility predicate doing its job, and the empty state says
so rather than implying the agency has no work.

**Ownership shows as an initials avatar inside the company cell**, not as a new
column. The table is `table-layout: fixed` with eight columns and three recent
commits spent fitting them to their content; a ninth column costs width the
layout does not have. A dashed empty circle means unassigned. The full name is
in the `title`, matching how the table already handles clamped text.

The detail panel gains:

- an owner line — a name, or "Unassigned" for a queue item
- **Claim**, when unassigned
- **Share**, for anyone who can see it
- **Assign**, for the owner and for an `owner`-role user

**Claim lives only in the detail panel**, not on list rows. Claiming is taking
responsibility for work, and it should follow reading the job order rather than
scanning a row at 9pm. The backend resolves the race with an atomic update and
returns 409 to the loser, so two people opening the same queue item is handled;
this is about the decision being informed, not about concurrency.

A **New job order** button opens the manual form: company, title, description,
salary, location, and an optional client. It posts `source: "manual"` and lands
assigned to its creator.

The client field is a **type-to-search box over the existing
`GET /api/clients?q=`**, not a preloaded dropdown. Clients are paginated and an
agency accumulates hundreds; members are 3–50 and load once. The two pickers
look similar and are not the same component, and conflating them would either
page the member list or preload every client. Leaving the field empty is
normal — a job order taken over the phone from a company you have not recorded
yet has no client, and `client_id` is nullable precisely for that.

### When a job order disappears under you

The detail panel can be showing a job order that stops being visible: a share is
withdrawn, or an owner reassigns it away. Any request the panel makes then
returns 404.

The panel treats that 404 as **"this closed"** rather than an error — it clears
the selection, shows a single line ("This job order is no longer available"),
and refreshes the list. It does not sit there displaying stale fields with a red
message beside them, which is what a generic error handler would produce, and it
does not silently blank, which reads as a bug.

**Cross-user live updates are out of scope.** `useLive` currently reacts to
`extraction` and `open` nudges only; there is no nudge for an assignment or a
share, so a colleague claiming a queue item will not move under your cursor. The
list refetches after any mutation you yourself make, and the 409 on a lost claim
refetches that row. Adding notification kinds for assign and share is a
worthwhile follow-up and is deliberately not in this round — it needs a
publisher change in the backend, and the feature is usable without it.

### Sharing

The dialog collects recipients through `member-picker`, an optional note, and an
optional broadcast.

The picker excludes the caller — sharing with yourself is a no-op the API
silently skips, and offering it invites the confusion.

**Broadcast to the whole agency is available only to the assignee and to an
`owner`-role user.** A share recipient may pass a job order on to a named
colleague but not throw someone else's client work open to the office. The
checkbox is disabled for them with that reason shown, rather than hidden: a
control that vanishes teaches nothing, and the rule is one worth understanding.

**On an unassigned job order there is no assignee, so only an owner may
broadcast.** This is the shipped API behaviour, not a new rule — the endpoint
gates on `can_edit`, which deliberately refuses unassigned rows, falling back to
the owner role. For everyone else the checkbox is disabled and says to claim it
first. That is the right answer as well as the true one: broadcasting work
nobody has taken responsibility for is a decision without an owner.

Below the picker, the people it is already shared with, each with **Remove**.
Removing revokes sight only — the API deletes a share row and nothing else, and
the confirmation says so, because "unshare" reads like it might delete their
work.

### Clients

The client panel gains an assigned-recruiter control using the same picker, and
a collaborators list.

Changing the assignee shows a checkbox — *"Move this client's job orders too"* —
defaulted on, matching the API. The response's `opportunities_moved` count is
surfaced afterwards: "12 job orders moved to Sarah." A reassignment that moves a
dozen job orders silently is the outcome the count exists to prevent.

The whole control is hidden unless the caller is the owner, the current
assignee, or the client is unassigned — mirroring the 403 the API returns, so
the interface does not offer an action that will be refused.

Collaborators are labelled as what they are: people who also know this account.
They grant no access to its job orders. Without that said plainly, the list
reads like a share.

## Errors

The backend distinguishes three failures carefully, and that distinction has to
survive into the interface. Collapsing them into "something went wrong" would
discard the reasoning the whole feature rests on.

| Status | What the screen says |
|---|---|
| 404 | "This job order is no longer available." It may never have been visible, or a share may have been withdrawn — and the message must not reveal which |
| 403 on an **assigned** job order | "This job order is shared with you, not assigned to you." |
| 403 on an **unassigned** one | "Claim this job order before editing it." The first message would be a lie here — nobody was assigned it |
| 409 on claim | "Someone else has taken this one." Then refresh the row |
| 401 | Existing behaviour — back to the landing page |

## Testing

Vitest and Testing Library, colocated with the component, following
`client-form.test.tsx`: stub `fetch`, assert the request body, assert what
renders.

- The picker excludes the signed-in user.
- Broadcast is unavailable to a share recipient, and the reason is shown.
- Claim renders 409 as "someone else has taken this one", not a generic error.
- 404 and 403 produce different messages.
- The reassign checkbox defaults on, and the moved count is reported.
- A queue row renders the dashed circle; an assigned row renders initials.
- The scope chips combine with the status chips rather than replacing them.
- `useMembers` fetches once across two mounts.
- The detail panel clears itself on a 404 rather than showing stale fields.
- The tab counts change with the scope chip, not only with the status chip.
- `/api/members` prefers `preferred_name` over `display_name`, and falls back to
  the email local-part when both are null.
- Broadcast is refused on an unassigned job order for a non-owner, and the
  message says to claim it first.
- A job order both assigned to you and broadcast returns `shared_with_me: true`
  — the flag means "a share reaches you", not "only a share reaches you".

## Deliberately not in this round

Bulk actions on multiple job orders. Sharing to a group or team. Share expiry.
Notification preferences for the two new event kinds — they dispatch already,
and the settings screen that would configure them is separate work. A
single-job-order route: the detail panel is a panel, and giving a job order its
own URL is a change to how the dashboard is navigated rather than part of this
feature.
