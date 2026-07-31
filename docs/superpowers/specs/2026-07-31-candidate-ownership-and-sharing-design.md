# Candidate ownership and sharing

Decided 2026-07-31. Amends the candidate model (§18-scoped tables) and extends
the sharing model established in
[the job order design](2026-07-30-job-order-assignment-and-sharing-design.md).

A candidate belongs to the recruiter who found them. Today the model says the
opposite: `Candidate` has no owner at all
([candidate.py:41](../../../backend/app/models/candidate.py)) — every candidate
is visible to every user in the tenant, and the only trace of who put them there
is the nullable `created_by` audit column. Job orders got ownership on
2026-07-30. Candidates did not, and the asymmetry is now the odd one out.

The second half of this design is harder and is the reason it needs writing
down. **Two recruiters can meet the same person and describe them differently.**
One records a candidate as a senior backend engineer asking $9k; the other, six
weeks later, as a tech lead open at $8k. Both are true accounts of the same
human, taken at different times through different conversations. The system has
to hold one person and two readings of them, without either recruiter losing
their own notes and without the agency counting one person as two.

## The collision this creates

These two requirements pull against each other, and the pull is the whole
design problem.

`email` and `phone_e164` are each unique per tenant, excluding merged rows
([candidate.py:217-230](../../../backend/app/models/candidate.py)), and
`find_candidate` resolves on exact email OR phone
([candidate_matching.py:58-113](../../../backend/app/services/candidate_matching.py)).
Today that is harmless: the second recruiter to type an email simply finds the
first recruiter's row, because everyone sees everything.

Make candidates private and the same constraint turns hostile. Recruiter B
types an email that already exists on a row B cannot see. Either the error
names a record B has no right to know about, or B is stopped by a wall with no
explicable cause. **This, not the share table, is the decision this spec
exists to make.**

## Decisions

| Question | Answer | Why |
|---|---|---|
| One row per person, or one per recruiter | One row | A person is one person. Per-recruiter rows make every headcount, dedup answer and "have we approached them before" answer wrong by construction, and permanently — there is no later migration back. |
| Per-tenant email/phone uniqueness | **Unchanged** | It is the only thing preventing the same person entering the database twice. Relaxing it to `(tenant_id, owner_id, email)` buys a clean create path at the cost of the guarantee. |
| Default visibility | The owner only | Same reasoning as job orders: private by default is what makes sharing a deliberate act. |
| Share scopes | `user` and `tenant` | Mirrors `opportunity_shares` exactly. |
| Share access level | Read only | One editor per record, as with job orders. |
| Colliding create | Not blocked — surfaced as "already registered by a colleague", with a request-access action | Argued below. |
| Divergent interpretation | A per-user override layer, not duplicate rows | Argued below. `CandidateFieldOverride` is the right shape but is **not** reusable as it stands. |
| Unowned candidates | Visible to everyone, claimable | The 2026-07-30 queue argument, unchanged: a queue nobody can see is a queue nobody clears. |
| Where visibility is enforced | Application query layer, not RLS | The 2026-07-30 argument, unchanged. |
| Elevated access | `role = 'owner'` sees everything | No new role vocabulary. |

Deferred: fuzzy/name-based duplicate detection, share expiry, team scopes,
per-recruiter private notes as a first-class type, ownership-transfer approval.

**On "similar to the client module":** the mechanism cannot come from clients.
`ClientCollaborator` ([client.py:228](../../../backend/app/models/client.py))
grants nothing and has no tenant scope — there is no share-to-everyone path
there at all. The only existing implementation of what was asked for is
`OpportunityShare`
([opportunity_share.py:28](../../../backend/app/models/opportunity_share.py)),
and this design mirrors it column for column so there is one sharing idiom in
the codebase rather than two.

## Data model

### `candidates`

| Column | Change | Notes |
|---|---|---|
| `owner_id` | **new** — composite FK `(tenant_id, owner_id)` → `users(tenant_id, id)`, `ON DELETE SET NULL (owner_id)`, nullable | NULL means the queue, not hidden. The composite form is mandatory for the reason the job order spec gives: a plain `users.id` reference would let a share reach across agencies. |

**The `SET NULL` must name its column** — `ON DELETE SET NULL (owner_id)`, not a
bare `SET NULL`. The bare form nulls `tenant_id` too, which is NOT NULL, so
deleting a recruiter fails outright instead of releasing their candidates. This
was found the hard way on `clients.assigned_user_id`; the note is repeated here
because this is a fourth composite `SET NULL` and the mistake is silent until a
recruiter leaves.

`created_by` stays. It is an audit column recording who typed the row, and it
must not drift into meaning ownership — ownership moves, authorship does not.

### `candidate_shares` (new)

One row per grant. Tenant-scoped, RLS-covered. Structurally identical to
`opportunity_shares`.

| Column | Notes |
|---|---|
| `candidate_id` | composite FK `(tenant_id, candidate_id)`, `ON DELETE CASCADE` |
| `scope` | `user` or `tenant`, pinned by `ck_candidate_shares_scope_known` |
| `shared_with_user_id` | composite FK, **`ON DELETE CASCADE`**. NULL if and only if `scope='tenant'`, enforced by `ck_candidate_shares_scope_target` |
| `shared_by_user_id` | composite FK, `ON DELETE SET NULL (shared_by_user_id)` |
| `note` | free text |
| `created_at` | from `Timestamps` |

Two partial unique indexes, so re-sharing updates rather than duplicating:

- `(tenant_id, candidate_id, shared_with_user_id)` where `scope = 'user'`
- `(tenant_id, candidate_id)` where `scope = 'tenant'`

**A share survives reassignment**, as it does for job orders. Handing a
candidate to another recruiter does not clear the shares — the colleagues who
were asked to look still have reason to. The outgoing owner keeps nothing
unless they are explicitly shared to, which leaves a record of that decision.

The CASCADE/`SET NULL` split is deliberate and is the same one the job order
spec argues: `SET NULL` on `shared_with_user_id` would silently convert a
targeted share into a tenant broadcast, and would in fact violate
`ck_candidate_shares_scope_target` and make deleting the user fail.

### `candidate_access_requests` (new)

The collision path needs somewhere to land, and a notification alone is not it —
a request that exists only as a notification cannot be answered twice, cannot be
listed, and cannot be shown as pending.

| Column | Notes |
|---|---|
| `candidate_id` | composite FK, `ON DELETE CASCADE` |
| `requested_by_user_id` | composite FK, `ON DELETE CASCADE` |
| `status` | `pending`, `granted`, `declined`, pinned by CHECK |
| `note` | why they are asking |
| `resolved_at`, `resolved_by_user_id` | `SET NULL` on the user |

Unique on `(tenant_id, candidate_id, requested_by_user_id)` where
`status = 'pending'` — one open request at a time, so a recruiter clicking twice
does not spam the owner.

Granting a request creates a `candidate_shares` row with `scope='user'` and
marks the request `granted`. The share is the grant; the request is the record
of how it came about.

## The colliding create

`POST /api/candidates` with an email or phone that resolves to a row the caller
cannot see returns **409 with a deliberately thin body**:

```json
{
  "detail": "already_registered",
  "candidate": { "full_name": "Wei Ming T.", "held_by": "Sarah Lim" },
  "can_request_access": true
}
```

Three things about that shape:

**It discloses something, and that is a decision, not an oversight.** The caller
learns this person is already in the agency's database and who holds them. The
alternative — a bare "cannot create" — is a wall a recruiter cannot act on, and
in a three-to-fifty person agency they will simply walk over to Sarah's desk and
ask. Encoding that conversation in the product is more honest than pretending
the row does not exist. The name is abbreviated and no contact detail, salary,
notes or client history crosses the boundary.

**Under PDPA this is worth a second look before it ships.** The disclosure is
internal to one agency and concerns data that agency already lawfully holds, so
the exposure is small — but "we hold this person" is itself information about a
data subject, and the retention rules in
[the ingestion design](2026-07-27-email-ingestion-design.md) apply to it. Flagged
here as a deliberate choice for the record, not settled by this spec alone.

**A conflicting match is not a collision.** `find_candidate` can return a
`conflict` when email and phone point at two different people
([candidate_matching.py:58-113](../../../backend/app/services/candidate_matching.py)).
That case stays exactly as it is today and does not offer request-access — the
system does not know which person is meant, so it cannot name one.

## Two readings of one person

Both recruiters keep their own account of the candidate through a per-user
override layer: a statement of judgement attributed to one recruiter, layered
over a shared base row that holds the facts.

**`CandidateFieldOverride` is the right shape and cannot be reused as it
stands.** It is unique on `(tenant_id, candidate_id, field_name)`
([candidate.py:600](../../../backend/app/models/candidate.py)) — one override
per field, last write wins, which is exactly the collision this section exists
to prevent. Its `changed_by` column
([candidate.py:589](../../../backend/app/models/candidate.py)) is a nullable
`SET NULL` audit trail, not an identity key, so it cannot carry the meaning
either.

It is also **live machinery with a different job**. `PATCH /candidates/{id}`
upserts an override on every hand-edit
([candidates.py:982](../../../backend/app/api/candidates.py)) to mean *a human
touched this field, so a later import must not overwrite it*, and
`undo.py:217` reads those rows. Widening the key to include a user id changes
what an import must check: "is this field protected" becomes "protected by
whom", and a rendered candidate becomes caller-dependent for the first time.

So the change is real work, in three parts, and none of it is optional:

1. `user_id` joins the unique key —
   `(tenant_id, candidate_id, user_id, field_name)`.
2. **The existing rows need a decided owner.** They were written to protect
   against imports, not to express one recruiter's view. Backfilling them to
   `changed_by` silently converts an agency-wide protection into one person's
   private opinion, and the next import would overwrite the field for everyone
   else. They must instead backfill to a NULL `user_id` meaning *tenant-wide
   protection*, with `user_id IS NULL` kept as a distinct, permanent tier.
3. Import protection then reads the NULL tier plus the row owner's tier;
   rendering reads the NULL tier plus the caller's.

What stays shared and single-valued: identity (name, email, phone), documents,
and activity history. What becomes per-recruiter: assessment fields. The line
has to be drawn explicitly, field by field, and that list belongs in the
implementation plan — but the rule that draws it is **fact stays shared,
judgement goes per-user.**

## Visibility

A new `visible_candidates(user_id, role)` clause in
[visibility.py](../../../backend/app/services/visibility.py), alongside the
opportunity predicate, returning a SQLAlchemy boolean clause so it composes with
the existing search, sort and pagination in `list_candidates`
([candidates.py:233](../../../backend/app/api/candidates.py)).

A user may see a candidate when any of these holds:

1. `owner_id IS NULL` — the unclaimed queue
2. `owner_id = :user_id`
3. a share exists with `scope = 'tenant'`
4. a share exists with `scope = 'user'` and `shared_with_user_id = :user_id`
5. `role = 'owner'`, which collapses the clause to TRUE

There is no mailbox term. Unlike job orders, candidates never arrive from the
email pipeline
([candidate.py:1-16](../../../backend/app/models/candidate.py)), so there is no
recipient with a prior claim.

Write access is narrower:

```
can_edit = owner_id = :user_id OR role = 'owner'
```

An unowned candidate is visible and claimable but not editable, for the reason
the job order spec gives: an unowned row is where a wrong edit is least likely
to be noticed.

`load_visible_candidate(...)` raises **404** when the predicate fails, and
`can_edit` raises **403** — the same split, for the same reasons.

### The routes that need guarding

`candidates.py` is **1,458 LOC against a 1,500 limit**, so nothing new goes in
it. Sharing and access-request endpoints get their own module.

| Route | Line | Check |
|---|---|---|
| `GET /candidates` | 233 | predicate in the query |
| `GET /candidates/{id}` | 492 | visibility |
| `POST /candidates` | 847 | collision path above |
| `PATCH /candidates/{id}` | 925 | **`can_edit`** |
| `POST /candidates/{id}/archive` | 1013 | **`can_edit`** |
| `POST /candidates/{id}/restore` | 1029 | **`can_edit`** |
| `POST /candidates/{id}/merge` | 1052 | **both sides** — see below |
| `POST /candidates/{id}/unmerge` | 1179 | **`can_edit`** |
| `DELETE /candidates/{id}` | 1230 | already owner-only; unchanged |
| `GET /candidates/{id}/export` | 1248 | visibility |
| `POST /candidates/{id}/activities` | 1281 | visibility — a share recipient may log an outreach |
| `GET /candidates/{id}/activities` | 1325 | visibility |

`/activities` is the one place a read-only recipient may write, and it is
deliberate: the row records that a WhatsApp message was opened, which is a fact
about what the recipient did, not an edit to the candidate.

**`PATCH` can collide too, and it is the easier one to forget.** Changing a
candidate's email or phone to a value held by an invisible row hits
`uq_candidates_tenant_email` at flush time and surfaces as a 500 with a
constraint name in it. The PATCH path therefore runs the same `find_candidate`
check as create, and returns the same thin 409 — including
`can_request_access`, because a recruiter correcting a typo has just as much
reason to discover the person is already held.

**Unmerge restores ownership as it was.** `POST /candidates/{id}/unmerge`
(l.1179) revives a row that was merged away; that row's `owner_id` survives the
merge untouched, so unmerging returns it to its original owner rather than to
whoever performed the unmerge. If that owner has since been deleted the column
is already NULL and the row lands in the queue, which is the same outcome every
other path gives.

A structural test mirrors
[test_opportunity_routes_guarded.py](../../../backend/tests/test_opportunity_routes_guarded.py):
no by-id route in `candidates.py` reads a `Candidate` without
`load_visible_candidate`, and no mutating by-id route writes one without
`can_edit`.

### Merge needs both sides

`POST /candidates/{id}/merge` (l.1052) today performs no visibility check at all
— tenant scope is the only boundary. With ownership that becomes a hole: merging
a visible candidate into an invisible one would hand the caller a row they were
never given, or destroy a colleague's record without their knowledge.

**Merge requires `can_edit` on both candidates.** The realistic case — recruiter
B discovers, after being granted access, that they and A hold the same person —
is not a merge but a request A performs, or an `owner` performs. A cross-owner
merge request is deferred; until it exists, the owner role is the escape hatch,
which is acceptable in a 3–50 person agency where the boss is one desk away.

## Imports

Import is where ownership is easiest to get silently wrong, and it was missed in
the first pass of this analysis. Candidates are **not** manual-only: bulk import
creates them at
[apply.py:357](../../../backend/app/services/imports/apply.py), and that
construction sets `import_id` but **no `created_by` and no user attribution at
all**.

1. `Candidate.owner_id` on import is set to `CandidateImport.uploaded_by`
   ([candidate.py:742](../../../backend/app/models/candidate.py)), which already
   records who ran the import — no new column is needed, only that `apply.py`
   stop discarding it. Without this an import produces a pile of unowned rows in
   the shared queue, which is the opposite of what someone uploading their own
   contact list intends.
2. **Import matching runs against the whole tenant, ignoring visibility.** It
   must, or an import would create a duplicate of an invisible row and violate
   the unique index at flush time with an unreadable error. Existing rows the
   importer cannot see are reported in the import summary in the same thin
   shape as the 409 above — "14 already registered by colleagues" — and are
   skipped, not updated.
3. **An import that would update a visible-but-not-editable row skips it.** A
   recruiter holding a share can see the candidate, so import matching resolves
   it, but they may not edit it — and an import is a bulk edit. It is reported
   in the summary alongside the invisible ones, worded as "held by a colleague"
   in both cases, since the two are indistinguishable to the importer and should
   read the same.
4. Import undo keys on `CandidateImportChange` rows, which record the entity and
   the field values. Undo must not revert a row whose owner has since changed;
   ownership is not an imported field.

## Migration

1. **`owner_id` backfills from `created_by`.** It is the closest honest answer
   available, and unlike the job order case there is no queue-visible argument
   for leaving everything NULL — every existing candidate has a person who typed
   it. Rows where `created_by` is NULL (imports, seeds) backfill to NULL and land
   in the claimable queue.
2. **Both new tables need `ENABLE ROW LEVEL SECURITY` and FORCE policies in the
   migration itself**, or `verify_rls_enforced`
   ([rls.py](../../../backend/app/db/rls.py)) refuses to boot the service.
3. `users` already carries `UniqueConstraint(tenant_id, id)`
   ([tenant.py:49](../../../backend/app/models/tenant.py), migration
   `20260730_1627_users_composite_key.py`), and `candidates` already carries
   `uq_candidates_tenant_id_id`
   ([candidate.py:192](../../../backend/app/models/candidate.py)), so both
   composite FK targets exist. No prerequisite migration is needed.

**The deploy is visible to users the moment it lands** — a database that was
wholly shared becomes wholly private. Every recruiter loses sight of every
candidate they did not create. That is the intended end state, but it should not
arrive unannounced; the rollout needs either a tenant-wide announcement or a
transitional period where existing rows carry a `scope='tenant'` share, decided
with the customer rather than in this document.

## API

### `app/api/candidate_shares.py` (new)

| Route | Behaviour |
|---|---|
| `POST /api/candidates/{id}/shares` | Body `{scope, user_ids?, note?}`. Caller must see the candidate; `scope='tenant'` restricted to the owner and `role='owner'`. Upserts against the partial unique indexes. |
| `DELETE /api/candidates/{id}/shares/{share_id}` | Owner, original sharer, `role='owner'`, or a recipient removing themselves. |
| `GET /api/candidates/{id}/shares` | Visible-only. |
| `POST /api/candidates/{id}/access-requests` | Creates a pending request. Callable **without** visibility — it is the one route reachable for an invisible candidate, and it returns nothing about the row. |
| `POST /api/candidates/{id}/access-requests/{req_id}/grant` | Owner or `role='owner'`. Creates the share. |
| `POST /api/candidates/{id}/access-requests/{req_id}/decline` | Same. Declining is a legitimate answer and produces no share. |

The access-request routes are the exception to the structural guard, and the
test must list them explicitly rather than letting a blanket exemption creep.

### Added to `app/api/candidates.py`

| Route | Behaviour |
|---|---|
| `POST /api/candidates/{id}/claim` | Only when `owner_id IS NULL`. Atomic `UPDATE ... WHERE owner_id IS NULL`; the loser of a race gets 409. |
| `POST /api/candidates/{id}/assign` | Body `{user_id \| null}`. Owner or `role='owner'`. `null` releases to the queue. |
| `GET /api/candidates?scope=` | `mine`, `queue`, `shared_with_me`, `all`. Default `all`. Filters **within** visibility; can never widen it. |

Both additions are small; if they push the file past 1,500 LOC, ownership routes
move out with the share routes rather than the limit being raised.

## Notifications

There are no candidate event kinds today —
[events.py:11-21](../../../backend/app/services/notify/events.py) carries four,
all opportunity-scoped. Six are added as constants, no migration:

| Kind | Recipients | Payload |
|---|---|---|
| `candidate.shared` | the new recipients only | sharer's name, the note |
| `candidate.assigned` | the new owner only | who assigned it |
| `candidate.unclaimed` | everyone | released to the queue |
| `candidate.access_requested` | the owner only | requester's name, the note |
| `candidate.access_granted` | the requester only | who granted it |
| `candidate.access_declined` | the requester only | who declined it |

`candidate.assigned` and `candidate.unclaimed` exist because the job order
design has their equivalents and the asymmetry would otherwise be silent:
releasing to the queue must tell the agency, or a released candidate is
available and nobody learns it.

`candidate.access_declined` is not optional politeness. A request that silently
never resolves is worse than a no — the requester goes on believing it is
pending and asks again.

A tenant broadcast is one event with N recipients, not N events, so the
per-subscriber hourly cap behaves as it does for job orders. Claiming from the
queue emits nothing — you did it, you know.

## Frontend

`frontend/app/dashboard/candidates/` needs five changes, and the first two are
the ones that make the feature comprehensible rather than merely enforced.

| Surface | Change |
|---|---|
| `page.tsx` | A `scope` filter — mine / queue / shared with me / all — matching the job order list. Without it, "all" silently means "the subset I can see" and a recruiter cannot tell private from shared. |
| `candidate-form.tsx` | The 409 path. A create that collides must render "already registered by Sarah Lim" with a **Request access** button, not a red validation error on the email field. This is the single most visible behaviour change in the release. |
| `candidate-panel.tsx` | Owner name, a share control, and read-only rendering when `can_edit` is false — a disabled edit affordance, not a hidden one, so a share recipient understands why. |
| new — request inbox | Pending access requests for candidates you own, with grant and decline. It can live in the panel or in `/settings`; it cannot live nowhere, or requests are only ever visible as a notification that scrolls away. |
| `candidates.ts` | Wraps the new share, claim, assign and access-request routes. All candidate API calls already go through this module and none are constructed inline — keep it that way. |

`candidate-whatsapp.tsx` needs no change: it posts to `/activities`, which a
share recipient is permitted.

## Testing

- The visibility predicate term by term, including `role='owner'` seeing a
  candidate owned by someone else.
- A cross-tenant share is refused by the composite foreign key, not merely by
  application code.
- Structurally: no by-id route in `candidates.py` reads a `Candidate` without
  `load_visible_candidate`, and no mutating one writes without `can_edit`.
- An invisible candidate returns 404 from `GET /candidates/{id}`, never 403.
- A share recipient is refused `PATCH` with 403, not 404.
- **Creating a candidate whose email matches an invisible row returns 409 with
  the thin body — and the response contains no phone, no salary, no notes, and
  no candidate id.**
- The same create where `find_candidate` returns `conflict` behaves as it does
  today and offers no request-access.
- An access request is creatable for an invisible candidate and leaks nothing;
  a second request from the same user does not raise on the partial unique index.
- Granting a request produces exactly one `scope='user'` share.
- `PATCH` changing an email to an invisible row's value returns the thin 409,
  not a 500 naming `uq_candidates_tenant_email`.
- Merge is refused when the caller can edit only one of the two candidates.
- Unmerge restores the revived row to its original owner, not to the caller.
- An override written by one recruiter does not change what another sees, and a
  pre-existing `user_id IS NULL` override still blocks an import for everyone.
- Releasing a candidate to the queue emits `candidate.unclaimed`; declining an
  access request notifies the requester.
- An import skips a row that is visible-but-shared as well as one that is
  invisible, and reports both.
- Import matching finds an invisible existing row, skips it, and reports it in
  the summary without creating a duplicate or violating `uq_candidates_tenant_email`.
- An import assigns `owner_id` to the importing user, not NULL.
- Deleting a recruiter drops their candidates into the queue rather than failing
  on `tenant_id` NOT NULL — the column-named `SET NULL`.
- Deleting a share recipient deletes their share rows without raising
  `ck_candidate_shares_scope_target`.
- Unsharing leaves the recipient's logged activities intact.
- `scope=mine|queue|shared_with_me|all` each return a subset of `all`.
- Two concurrent claims: one 200, one 409.
- The migration backfills `owner_id` from `created_by` and leaves imported rows
  NULL.
