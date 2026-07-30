# Clients administration — add, edit, suspend, administer

Decided 2026-07-30.

Today a client row exists only because the pipeline proposed it. `clients.py`
has no create and no edit: every write endpoint is a state transition a human
makes about a row a matcher made. That is the right default and it stays. But
an agency's client list is its most valuable asset, and a recruiter cannot
currently type one in, correct a name, record a fee, or put a firm on hold.

This design adds the human-authored half of the table without weakening the
provenance rules that the pipeline half depends on.

## Decisions

| Question | Decision |
|---|---|
| What `suspend` means | A new status. A **live** client temporarily on hold — unpaid invoice, contract dispute. Distinct from `archived`, which is permanent. |
| What may be edited | Name, email domain, firm details, commercial terms, notes, contacts. |
| Who may do it | Any user in the tenant. No role model is introduced. |
| Status of a hand-created client | `confirmed`. |
| What suspension blocks | Candidate submissions (409). Sourcing runs stay allowed. |
| Where the new fields live | Flat columns on `clients`; contact **people** in a new `client_contacts` table. |

## Data model

### `clients` — new columns

| Column | Type | Notes |
|---|---|---|
| `website`, `phone`, `address` | `Text` null | Firm-level, not person-level. |
| `fee_percent` | `Numeric(5, 2)` null | A percent, because that is what a recruiter quotes: `20.00`. |
| `payment_terms_days` | `Integer` null | |
| `notes` | `Text` null | Internal free text. |
| `suspended_reason` | `Text` null | |
| `suspended_at` | `timestamptz` null | |
| `source` | `String(16)` not null, default `pipeline` | `pipeline` \| `manual`. |

`source` cannot be inferred from `first_seen_email_message_id`: that column is
`ON DELETE SET NULL` ([client.py:58](../../../backend/app/models/client.py)),
so a retention purge would silently reclassify a pipeline client as manual.

**Cut as YAGNI — decided, not deferred:** `owner_user_id` and
`contract_start`/`contract_end`. An owner column would need a new
`(tenant_id, id)` unique constraint on `users` to carry a tenant-safe composite
FK — [tenant.py:42-44](../../../backend/app/models/tenant.py) has only
`(tenant_id, email)` and `(tenant_id, ms_object_id)` — and nothing reads an
owner. The permission use-case is also gone by construction: this design lets
*any* user in the tenant do everything, and on a personal-account tenant
(`tenants.is_personal_account`) there is only one user for an owner column to
point at. Re-open only when a feature actually reads an account manager, and
add the constraint in that migration.

### `client_contacts` — new table

`UUIDPrimaryKey`, `TenantScoped`, `Timestamps`, like every other business table
(§18). Columns: `client_id`, `name` (not null), `email`, `phone`, `title`,
`is_primary` (not null, default false).

- Composite FK `(tenant_id, client_id) → clients(tenant_id, id)`,
  `ON DELETE CASCADE` — mirroring `client_mentions`
  ([client.py:112-118](../../../backend/app/models/client.py)) so a contact
  cannot cross agencies.
- Partial unique index on `(tenant_id, client_id) WHERE is_primary` — at most
  one primary contact per client. A partial unique **index** cannot be
  `DEFERRABLE` (only constraints can, and a partial unique constraint does not
  exist in Postgres), so write *order* is the safety mechanism: the demote
  statement (`UPDATE ... SET is_primary = false` on the current primary) must
  execute **before** the statement that promotes or inserts the new primary.
  A unique index is checked at the end of each statement, so demote-then-
  promote never has two `true` rows visible to a check; the reverse order
  fails mid-transaction.
- **RLS: ENABLE + FORCE + `tenant_isolation` policy in the migration**, using
  the same `_enforce_rls` loop as the migration that created `clients`
  ([20260728_1100_client_profiles.py:150-166](../../../backend/alembic/versions/20260728_1100_client_profiles.py))
  — just add `("client_contacts", "tenant_id")` to that migration-local
  `PROTECTED` pattern. No `GRANT` is needed: the base RLS migration's
  `ALTER DEFAULT PRIVILEGES` ([20260726_1800_row_level_security.py:73-78](../../../backend/alembic/versions/20260726_1800_row_level_security.py))
  already gives the runtime role DML on every new table — which is exactly why
  the policy line is the dangerous omission: the role can read the table the
  moment it exists. Two backstops catch forgetting it: `verify_rls_enforced()`
  ([rls.py:58](../../../backend/app/db/rls.py)) refuses to boot over any
  readable table lacking FORCE RLS, and `tests/test_rls.py` runs against the
  migrated schema. `tests/conftest.py` toggles nothing — RLS comes from the
  migrations — but its `_CLEANUP_STATEMENTS` needs a
  `DELETE FROM client_contacts WHERE tenant_id = :t` line before the `clients`
  delete: the cascade covers the happy path, but each cleanup statement runs
  in its own transaction precisely so debris survives a partial failure.

Contacts are deleted outright rather than status-flagged. Unlike a
`client_mention`, a contact is not evidence of something that happened; it is a
current fact about who to call, and a stale one is worse than an absent one.

## Status machine

```
unconfirmed ──confirm──> confirmed ──suspend──> suspended
     ↑                        │  ↑                  │
     │                        │  └────unsuspend─────┘
     │                     archive              archive
     │                        ↓                     ↓
     └────restore────────── archived ←──────────────┘
```

- **Suspend** only from `confirmed` (and `suspended`, idempotently). A client
  that was never confirmed cannot be put on hold — that case is `archive`.
- **Unsuspend** returns to `confirmed`, **not** `unconfirmed`. This differs from
  `restore` on purpose. `restore` sends a row back through review because
  archiving revoked the judgement that the agency currently works with this
  firm ([clients.py:203-209](../../../backend/app/api/clients.py)). A
  suspension revokes nothing — the agency still works with the firm, it is
  simply not sending candidates this month. Re-review would be noise.
- **Archive** accepts `suspended` as a source, so a hold that becomes permanent
  needs no unsuspend hop first.
- Both `archive` and `unsuspend` clear `suspended_at` and `suspended_reason`.
- Concretely: `_LEGAL_SOURCES` gains a `SUSPENDED` key with
  `{confirmed, suspended}`, and its `ARCHIVED` set gains `suspended`. But
  `suspend`/`unsuspend` are not plain `_transition` calls — they set and clear
  the two suspend columns — and `archive` from `suspended` must clear them
  too, so `_transition` grows an optional extra-values argument rather than
  the endpoints duplicating it.
- `suspended` is a live client: it counts inside the `all` chip and appears in
  the default listing, unlike `merged`.
- `_LEGAL_SOURCES` does **not** gain `suspended` under `CONFIRMED` — unsuspend
  is the only exit. But the generic refusal message at
  [clients.py:436-442](../../../backend/app/api/clients.py) would then read
  *"Client is suspended; restore it before marking it confirmed"*, naming the
  wrong endpoint. `confirm` special-cases suspended to *"Unsuspend the client
  first"*, exactly as it already special-cases `merged` at line 434.

### The matcher is unaffected, and here is why

`_BY_DOMAIN` ([client_matching.py:36](../../../backend/app/services/client_matching.py))
matches any non-deprioritised status, and `_surviving` (line 178) never writes
`status`. So an inbound email for a suspended client finds the row, touches
`last_seen_at`, and leaves it suspended. The insert path's `ON CONFLICT`
predicate is `status <> 'merged'` (line 64), which still covers a suspended row
— the same reasoning as the archived-row comment at
[client.py:75-78](../../../backend/app/models/client.py). Suspension is a
commercial state, not an identity state; the matcher has no opinion about it.
A test asserts this rather than leaving it to the reader.

## API

All five additions live in `clients.py`, following the `CandidateIn` /
`CandidateUpdate` pair at
[candidates.py:756](../../../backend/app/api/candidates.py) and
[candidates.py:790](../../../backend/app/api/candidates.py).

| Endpoint | Body | Behaviour |
|---|---|---|
| `POST /clients` | `ClientCreate` — `name` required, rest optional | Inserts `status=confirmed`, `source=manual`. `name_normalized` comes from `normalize_company_name` in [client_naming.py](../../../backend/app/services/client_naming.py) — the module the matcher itself imports, not `client_matching` — so the API and the pipeline agree by construction. `client_naming` imports only `re` and `settings`; no import cycle. |
| `PATCH /clients/{id}` | `ClientUpdate` — every field optional | Edits name, domain, firm and commercial fields, notes. Refuses a `merged` row (400, "unmerge first"). Re-derives `name_normalized` when `name` changes. |
| `POST /clients/{id}/suspend` | `{reason?}` | From `confirmed`/`suspended`. Sets `suspended_at`, `suspended_reason`. |
| `POST /clients/{id}/unsuspend` | — | From `suspended` only → `confirmed`. Clears both suspend columns. |
| `POST /clients/{id}/contacts`, `PATCH`/`DELETE .../contacts/{contact_id}` | `ContactIn` / `ContactUpdate` | Setting `is_primary` demotes the existing primary in the same transaction, demote statement first — the partial unique index makes that order mandatory, not a nicety (see the data-model section). |

### `email_domain` obeys the pipeline's own rules

The pipeline never stores a free-provider domain — `domain_of`
([client_naming.py](../../../backend/app/services/client_naming.py)) returns
None for anything in `settings.FREE_EMAIL_DOMAINS` because such a domain
identifies a person, not a company. The API must not be a back door: `POST`
and `PATCH` lowercase-and-strip the value, then **422** on a free-provider
domain. Accepting `gmail.com` would claim the tenant's one unique slot for it
and make every Gmail sender in the tenant's mail match this client.

`PATCH` with an explicit `"email_domain": null` **clears the domain**. That is
a legitimate edit — "we got this domain wrong" — and the row simply falls back
to name-only matching, the same state every pipeline row with a free-provider
sender is already in.

### Domain collisions are a 409, never an adoption

`uq_clients_tenant_domain` excludes only `merged`, so a recruiter hand-adding
"Acme" on `acme.com` when the pipeline already holds an unconfirmed row for
that domain will violate it. Both `POST` and `PATCH` catch the `IntegrityError`,
narrow it with the SQLSTATE-23505 check that already exists as `_is_duplicate`
([candidates.py:632](../../../backend/app/api/candidates.py)) — asyncpg
surfaces a unique violation as `exc.orig.sqlstate == "23505"`, proven by the
create-candidate path at line 888 — hoisted into a shared helper rather than
imported from `candidates`. The narrowing is unambiguous here: with `id`
server-generated and `status` fixed, the domain index is the only constraint
on `clients` that can raise 23505 from these writes; anything else is a bug
and is re-raised, exactly as candidates.py argues at 890-900.

The response is **409 naming the existing client, its status, and the next
step** — confirm and edit that row. The `IntegrityError` itself does not carry
that; after rollback, the holder is fetched by domain in a fresh
`tenant_session` to build the message. Same shape as the unmerge 409 at
[clients.py:388-395](../../../backend/app/api/clients.py).

Adopting the existing row silently would make "Add client" sometimes mean "edit
a row you did not know existed", and the recruiter would never learn that the
pipeline had already found the firm.

### There is no `DELETE /clients/{id}` — not even for manual, mention-free rows

Every removal in this codebase is a status change. `client_mentions` are
explicitly never deleted, because an absent mention asserts "this never
happened"
([client.py:91-96](../../../backend/app/models/client.py)). A client carrying
mentions and submissions is the same case. `archive` is the delete.

A hard delete restricted to `source=manual` rows with zero mentions was
considered and **rejected**. "Zero mentions" is not a stable property — the
matcher attaches a mention to a manual row the moment a matching email
arrives, and mentions are not the only references: `candidate_submissions` and
`sourcing_runs.client_id`
([20260729_1800_sourcing_run_client.py](../../../backend/alembic/versions/20260729_1800_sourcing_run_client.py))
both FK the row. The endpoint would need a three-table emptiness check to
avoid a raw FK violation, all to serve one case — an immediate fat-finger —
that `PATCH` (fix the typo) or `archive` (put it away) already covers. One
uniform rule, "removal is a status change", is worth more than a tidy list.

### Where suspension gets its teeth

`POST /candidates/{id}/submissions`
([sourcing.py:274](../../../backend/app/api/sourcing.py)) loads the client and
returns **409 with the suspension reason** if it is suspended. Submitting a
candidate is the outward-facing act, and it is the one that must not happen
while a client is on hold.

`POST /opportunities/{id}/sourcing` (line 128) is untouched. Ranking and
shortlisting are internal research; blocking them would stop a recruiter
preparing for the day the hold lifts.

### Serialisation

`StatusFilter` ([clients.py:27](../../../backend/app/api/clients.py)) gains
`"suspended"`. `_serialize` gains every new column. `contacts` are returned by
`GET /clients/{id}` alongside `mentions` — not by the list endpoint, which
stays one row per client.

## Frontend

`client-panel.tsx` is 455 lines, so the form does not go inside it.

| File | Change |
|---|---|
| `clients/client-form.tsx` (new) | One component for create and edit, mirroring `candidates/candidate-form.tsx`. Opened from the existing `dashboard/dialog.tsx`. Contacts are added and removed inline. |
| `clients/page.tsx` | **Add client** button beside the status chips; a `suspended` chip. |
| `clients/clients-table.tsx` | Suspended badge — amber, distinct from archived's muted grey. |
| `clients/client-panel.tsx` | Read-only **Details** section (commercial terms, contacts, notes) with an Edit button; Suspend / Unsuspend in the action row; the suspension reason and date at the top when suspended. |
| `dashboard/clients.ts` | `createClient`, `updateClient`, `suspendClient`, `unsuspendClient`, and the three contact calls, beside the existing seven wrappers. The `Client` type grows to match `_serialize`. |

A suspension the recruiter cannot see is a suspension they will trip over. The
submissions UI surfaces the 409 as the stored reason, not as a generic failure.

## Tests

Extending `test_clients_api.py` and its `agency_with_clients` fixture:

- create happy path; create 409 on domain collision (response names the
  holder); create with no domain; create with a free-provider domain (422).
- `PATCH` name (re-normalises), `PATCH` domain into a taken slot (409),
  `PATCH` domain to explicit null (cleared), `PATCH` domain to a free-provider
  domain (422), `PATCH` a merged row (400).
- suspend from confirmed; suspend from unconfirmed (400); suspend idempotent;
  unsuspend → confirmed; unsuspend a non-suspended row (400); archive from
  suspended clears the suspend columns; confirm on suspended says
  "unsuspend", not "restore".
- `suspended` counted in `all` and present in the default listing.
- contacts: create, primary demotion (post two primaries in sequence, assert
  exactly one `is_primary` row survives), delete, cross-tenant 404 on read,
  patch **and** delete — this trio, run through the runtime role, is what
  actually exercises the new table's RLS policy beyond the structural
  `verify_rls_enforced()` boot check.
- `_CLEANUP_STATEMENTS` in `conftest.py` gains the `client_contacts` line —
  the fixture teardown, not a test, but forgetting it strands debris on any
  partial cleanup.
- the matcher touches a suspended client and leaves its status alone.
- submission to a suspended client is a 409 carrying the reason; a sourcing run
  against the same client succeeds.
