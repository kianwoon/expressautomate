# Per-tenant client and candidate profiles

Decided 2026-07-28. Backend only — `frontend/` is still empty, so no UI ships
with this.

Every agency keeps its own list of the companies it recruits for and the people
it places. Today neither exists as a record: client identity is free text on
`opportunities.company_name_raw` / `company_name_normalized`, and candidates are
not modelled at all. This spec adds both as first-class tenant-scoped tables,
proposed automatically from ingested email and confirmed by a human.

## Decisions

| Question | Answer |
|---|---|
| How profiles appear | Auto-proposed at ingestion, `unconfirmed` until a human confirms |
| Table shape | Two tables — `clients` and `candidates`, not one polymorphic table |
| Match key | Client: hiring-contact email **domain**. Candidate: email **address**. Normalized name is a proposal hint only, never an automatic match |
| Candidate retention | Per-tenant window, reusing the existing email retention config |
| Scope | Models, migration, RLS, matcher service, API, tests |

Clients and candidates are different enough — a client has a domain and hiring
contacts, a candidate has a title and years of experience — that a shared table
would be null-heavy and could not express constraints like "a candidate needs an
email". The shared identity spine (status, merge target, timestamps) is small
enough to duplicate.

## Data model

Four new tables. Each carries `tenant_id` via the `TenantScoped` mixin
(`app/db/base.py:33`) and must repeat `ENABLE` + `FORCE ROW LEVEL SECURITY` and
the tenant policy in its own migration — RLS is opt-in per table, and
`verify_rls_enforced()` (`app/db/rls.py:88`) fails at startup for any business
table that skips it.

### `clients`

| Column | Notes |
|---|---|
| `id`, `tenant_id` | |
| `name` | display name as first seen |
| `name_normalized` | lowercased, legal suffix stripped — a match hint, **not** a key |
| `email_domain` | nullable; NULL when the contact's domain is a free provider |
| `status` | `unconfirmed` \| `confirmed` \| `merged` \| `archived`, CHECK constrained |
| `merged_into_client_id` | nullable self-FK; CHECK: NOT NULL iff `status = 'merged'` |
| `first_seen_email_message_id` | nullable, `ON DELETE SET NULL` |
| `last_seen_at`, `created_at`, `updated_at` | |

Unique index: `(tenant_id, email_domain)` partial
`WHERE email_domain IS NOT NULL AND status <> 'merged'`.

There is **no** unique index on `name_normalized`. Two unrelated firms
legitimately normalize to the same string, and an archived client holding the
slot would make a new proposal fail to insert.

Excluding `merged` from the domain index matters: without it a merged client
keeps its domain reserved, and merging one domain-keyed client into another
becomes impossible.

### `candidates`

The same identity spine, keyed on `email` (unique per tenant, partial
`WHERE status <> 'merged'`) rather than a domain, plus nullable `current_title`,
`years_experience`, and `location`.

No `notes` column and no skills or salary sub-tables. Free-text notes invite
personal data with no retention story; the rest is speculative until a UI asks
for it.

### `client_mentions` and `candidate_mentions`

`(tenant_id, client_id | candidate_id, email_message_id, matched_by, confidence,
created_at)`, where `matched_by` is CHECK constrained to
`email_domain | email_address | name | human`.

Provenance lives here, never on the profile row — the profile is the
human-facing record, the mentions are the evidence trail. This follows the
convention that AI-derived data keeps evidence, confidence, and model version
separate from the record it produced.

`email_message_id` is `ON DELETE SET NULL`, not CASCADE. Retention purging an
email body must not silently erase the reason a profile exists.

No `extraction_id` column — `extraction_evidence` already models the link from
an extraction to a field value.

### Cross-tenant foreign keys

RLS policies do **not** filter foreign-key validation. Nothing at the database
level stops a mention row in agency A pointing at agency B's `client_id`.

Every FK between these tables is therefore composite — `(tenant_id, client_id)`
referencing `clients (tenant_id, id)` — which requires a unique constraint on
`(tenant_id, id)` on each parent. A test must assert that the cross-tenant
insert fails.

### What is deliberately not touched

`opportunities.company_name_raw` stays as it is. It is raw provenance, not a
denormalized cache, and rewriting it would destroy the record of what the email
actually said. No `candidate_id` is added to `opportunities`: an opportunity is
a job order, and linking applicants to job orders is a separate feature.

## Matching

One service, `app/services/profile_matching.py`, invoked after extraction lands,
alongside opportunity construction. For each mention it resolves in order and
stops at the first hit.

1. **Exact key.** Candidate: `email`. Client: `email_domain`, unless that domain
   is in the configured free-provider set. A hit links to the existing profile,
   records `matched_by = 'email_address' | 'email_domain'`, and touches
   `last_seen_at`. No new row and no human step.
2. **Normalized name.** A hit records a mention against the existing profile but
   leaves it `unconfirmed` and surfaces it for review. `matched_by = 'name'`.
   A name never confirms a profile on its own.
3. **No hit.** Insert a new `unconfirmed` profile.

**Free email domains.** `gmail.com`, `hotmail.com` and similar would otherwise
collapse every unrelated client into one record. The set is a setting on
`app.core.config.settings`, not a literal in source. On a hit, `email_domain` is
stored as NULL and matching falls through to the name proposal.

Multi-entity groups that genuinely share one domain collapse into a single
client. That is accepted for v1: a recruiter can archive the wrong one and
create the sibling by hand. No split operation ships.

The matcher never fabricates a field: an absent value stays NULL rather than
being inferred (§15).

**Confirm and merge are human-only** (`matched_by = 'human'`). Merge repoints
mentions to the target, sets `status = 'merged'` with `merged_into_*_id`, and
refuses cycles.

Everything runs inside `tenant_session(tenant_uuid)`, so a matcher bug cannot
reach across tenants — an unscoped session sees zero rows rather than another
agency's data.

## Retention

Candidate profiles are personal data. They reuse the existing per-tenant
retention window that already governs raw email: a candidate whose `last_seen_at`
is older than the window is purged along with its mentions.

Clients are exempt — company identity is not personal data.

## API

Under `/api`, following `opportunities.py` exactly: `_require_session` →
`tenant_session` → `limit` / `offset` clamped (not rejected) from settings,
response `{items, total, limit, offset, counts}`.

- `GET /api/clients`, `GET /api/clients/{id}` — detail includes mentions
- `POST /api/clients/{id}/confirm`, `POST /api/clients/{id}/merge`
- The same four under `/api/candidates`, plus `DELETE /api/candidates/{id}`

Status counts are computed outside the filter window so the chips do not shift
while paging — the convention at `app/api/opportunities.py:104`.

## Testing

Against the throwaway Postgres container, never the live database.

- **Isolation.** Agency A cannot read, update, or FK-reference agency B's
  profiles. The FK case is written first and must fail before the composite FKs
  exist.
- **Matching.** Exact-key reuse; free-domain fallback to name; a name match
  proposes without confirming; no hit creates an `unconfirmed` profile.
- **Merge.** Mentions repoint, the domain index frees up, cycles are rejected,
  the `status`/`merged_into` CHECK holds.
- **Retention.** A stale candidate purges; its client does not; a purged email
  leaves the profile and its mention standing with a NULL message id.
- **Routing.** `tests/test_routing.py` already fails if a route escapes `/api`.
