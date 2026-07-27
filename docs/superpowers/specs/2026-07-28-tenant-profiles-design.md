# Per-tenant client profiles

Decided 2026-07-28. Backend only — `frontend/` is still empty, so no UI ships
with this.

Every agency keeps its own list of the companies it recruits for. Today that
identity is free text on `opportunities.company_name_raw` /
`company_name_normalized`; there is no record of a client as such. This spec
adds one, proposed automatically from ingested email and confirmed by a human.

## Scope: clients only

Candidate profiles were part of the original plan and have been split into a
separate spec. They cannot be built yet: the extraction schema `ExtractedJob`
(`app/services/ingest/schema.py:72`) carries only company and job fields — no
candidate name, email, title, or years of experience — so there is nothing to
propose a candidate from. The only email address in the pipeline is
`email_messages.sender_email` (`app/models/email_message.py:94`), which
identifies the sender, not a candidate.

The candidate spec must cover extending the extraction schema and prompt, the
evidence and confidence handling for the new fields, re-extraction of
already-ingested email, and PDPA retention for personal data. None of that is in
this document.

Clients need none of it. A client is proposed from the sender's email domain,
which every ingested message already carries.

## Decisions

| Question | Answer |
|---|---|
| How profiles appear | Auto-proposed at ingestion, `unconfirmed` until a human confirms |
| Match key | Sender email **domain**. Normalized name is a proposal hint only, never an automatic match |
| Scope | Model, migration, RLS, matcher, API, tests — clients only |

## Data model

Two new tables. Each carries `tenant_id` via the `TenantScoped` mixin
(`app/db/base.py:33`) and must repeat `ENABLE` + `FORCE ROW LEVEL SECURITY` and
the tenant policy in its own migration — RLS is opt-in per table, and
`verify_rls_enforced()` (`app/db/rls.py:88`) fails at startup for any business
table that skips it.

### `clients`

| Column | Notes |
|---|---|
| `id`, `tenant_id` | unique on `(tenant_id, id)` so children can use composite FKs |
| `name` | display name as first seen |
| `name_normalized` | lowercased, legal suffix stripped — a match hint, **not** a key |
| `email_domain` | nullable; NULL when the sender's domain is a free provider |
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
becomes impossible. `archived` deliberately stays *inside* the index — see
"Reprocessing" below.

### `client_mentions`

`(tenant_id, client_id, email_message_id, matched_by, created_at)`, where
`matched_by` is CHECK constrained to `email_domain | name | human`.

Unique on `(tenant_id, client_id, email_message_id)`. Extraction re-runs on the
same message after a crash, and replay appends; without this constraint every
rerun duplicates the mention.

No `confidence` column. `matched_by` already encodes match strength honestly —
a domain match is a fact about where the mail came from, a name match is a
resemblance. A per-mention numeric score would be a fabricated probability,
which this codebase deliberately avoids (see the module docstring in
`app/api/opportunities.py` on never rendering `model_confidence` as one).

Provenance lives here, never on the profile row — the profile is the
human-facing record, the mentions are the evidence trail.

`email_message_id` is `ON DELETE SET NULL`, not CASCADE, so purging an email body
cannot silently erase the reason a profile exists.

No `extraction_id` column — `extraction_evidence` already models the link from
an extraction to a field value.

### Cross-tenant foreign keys

RLS policies do **not** filter foreign-key validation. Nothing at the database
level stops a mention row in agency A pointing at agency B's `client_id`.

Every FK between these tables is therefore composite — `(tenant_id, client_id)`
referencing `clients (tenant_id, id)`. A test must assert the cross-tenant
insert fails.

### What is deliberately not touched

`opportunities.company_name_raw` stays as it is. It is raw provenance, not a
denormalized cache, and rewriting it would destroy the record of what the email
actually said. No `client_id` FK is added to `opportunities` in this spec —
linking job orders to clients is the obvious next step but belongs with the UI
that would use it.

## Matching

One service, `app/services/client_matching.py`, called from `persist()`
(`app/services/ingest/persist.py:116`), which `extract_email` invokes
(`app/workers/jobs.py:618`). For each ingested message it resolves in order and
stops at the first hit.

1. **Sender domain.** Unless that domain is in the configured free-provider set,
   look up `clients.email_domain`. A hit links to the existing client, records
   `matched_by = 'email_domain'`, and touches `last_seen_at`.
2. **Normalized name** from the extracted company name. A hit records a mention
   against the existing client but leaves it `unconfirmed` and queued for
   review. `matched_by = 'name'`. A name never confirms a client on its own.
3. **No hit.** Insert a new `unconfirmed` client.

**NULL sender.** `sender_email` is nullable. A message without one skips step 1
entirely and falls to name matching; it never fabricates a domain (§15).

**Free email domains.** `gmail.com`, `hotmail.com` and similar would otherwise
collapse every unrelated client into one record. The set is a setting on
`app.core.config.settings`, not a literal in source. On a hit, `email_domain` is
stored as NULL and matching falls through to the name proposal.

Multi-entity groups that genuinely share one domain collapse into a single
client. That is accepted for v1: a recruiter can archive the wrong one and
create the sibling by hand. No split operation ships.

### Reprocessing

`extract_email` legitimately re-runs — it accepts messages already in
`extracting` state after a crash (`app/workers/jobs.py:586`), and replay appends.
The matcher must therefore be idempotent:

- The unique on `(tenant_id, client_id, email_message_id)` makes a repeated
  mention a no-op rather than a duplicate.
- **Step 1 matches archived and merged clients too.** An archived client keeps
  its row in the domain index, so skipping it would send the matcher to step 3,
  where the insert hits that same index and crashes. On a merged client the
  matcher follows `merged_into_client_id` to the surviving row.
- A match **never changes status**. Re-seeing an archived client does not
  resurrect it; it records a mention and updates `last_seen_at`. Un-archiving is
  a human action.

**Confirm and merge are human-only** (`matched_by = 'human'`). Merge repoints
mentions to the target, sets `status = 'merged'` with `merged_into_client_id`,
and refuses cycles.

Everything runs inside `tenant_session(tenant_uuid)`, so a matcher bug cannot
reach across tenants — an unscoped session sees zero rows rather than another
agency's data.

## Retention

Clients are company identity, not personal data, so no retention window applies
to them.

One pre-existing gap is recorded here because it was found while designing this
feature, and because the candidate spec will depend on it: **retention is
written but never enforced.** `mailboxes.retention_months`
(`app/models/mailbox.py:51`) and per-row `email_messages.retention_until`
(`app/models/email_message.py:121`) are populated at ingestion
(`app/workers/jobs.py:80,126`) and nothing ever reads them. No purge job or cron
deletes expired rows.

That worker is separate work, covering email bodies and — once candidates exist
— candidate profiles. This spec does not build it. `client_mentions` uses
`ON DELETE SET NULL` so it will survive that purge when it arrives.

## API

Under `/api`, following `opportunities.py` exactly: `_require_session` →
`tenant_session` → `limit` / `offset` clamped (not rejected) from settings,
response `{items, total, limit, offset, counts}`.

- `GET /api/clients` — `status` query filter; defaults to excluding `merged`.
  This is also the review queue: `?status=unconfirmed` is what a recruiter works
  through.
- `GET /api/clients/{id}` — detail includes mentions
- `POST /api/clients/{id}/confirm`
- `POST /api/clients/{id}/merge` — body names the target
- `POST /api/clients/{id}/unmerge` — restores a wrongly merged client, since
  merge is destructive to the mention graph and recruiters will get it wrong
- `POST /api/clients/{id}/archive`

Status counts are computed outside the filter window so the chips do not shift
while paging — the convention at `app/api/opportunities.py:104`.

## Testing

Against the throwaway Postgres container, never the live database.

- **Isolation.** Agency A cannot read, update, or FK-reference agency B's
  clients. The FK case is written first and must fail before the composite FKs
  exist.
- **Matching.** Domain reuse; free-domain fallback to name; NULL sender skips to
  name; a name match proposes without confirming; no hit creates an
  `unconfirmed` client.
- **Reprocessing.** Re-running `extract_email` on the same message adds no
  second mention; re-seeing an archived client records a mention without
  un-archiving it; re-seeing a merged client attaches to the merge target.
- **Concurrency.** Two workers processing different messages from the same new
  domain simultaneously produce one client, not a unique violation — the loser
  retries onto the winner's row.
- **Merge.** Mentions repoint, the domain index frees up, cycles are rejected,
  unmerge restores, the `status`/`merged_into` CHECK holds.
- **Config.** The free-domain set comes from settings and has a sane default;
  nothing in source hardcodes a provider list.
- **Routing.** `tests/test_routing.py` already fails if a route escapes `/api`.
