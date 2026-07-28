# Per-tenant candidate profiles

Decided 2026-07-28. Backend and UI.

Every agency keeps its own list of the people it places. This is the counterpart
to [client profiles](2026-07-28-tenant-profiles-design.md), and it is built
differently for one reason: **candidates do not come from email.**

## Why nothing is extracted

The client spec deferred candidates on the grounds that the extraction schema
carried no candidate fields. That is still true, and it is not the real
obstacle. Three are:

- **The classifier is binary.** `app/services/ingest/classify.py:28` asks only
  `is_job_order: true/false`, and `should_extract()` (`classify.py:80`) returns
  False for `non_recruitment`. A CV email is not merely unextracted today — it
  is dropped before extraction runs.
- **Attachments are unreachable.** `email_messages` stores a `has_attachments`
  boolean (`app/workers/jobs.py:1078`) and nothing else. No code downloads or
  parses an attachment, so a CV's contents do not exist in this system.
- **The prompt asks for vacancies** (`app/services/ingest/extract.py:32`).

Rather than build all three, the recruiter enters candidates by hand or uploads
a spreadsheet — which is how these agencies already hold their candidate lists.
No classifier change, no attachment pipeline, no prompt change, no AI.

That also means **nothing here is AI-derived**, so none of the evidence,
confidence, or provenance machinery around extractions applies. Every value has
a human author, which is a stronger claim than any extraction can make.

## Decisions

| Question | Answer |
|---|---|
| Where candidates come from | Manual entry, or CSV/XLSX upload |
| Identity | Email **or** phone matches — either one is enough |
| On re-import conflict | The import wins, except on a field a human edited |
| Scope | Backend and UI, in two phases |

## Build order

Two phases. Each is independently shippable, and the order is deliberate: the
data model and the dedupe rule get proven by real use before the risky part is
built on them.

1. **Candidates core** — tables, API, and screens: list, detail, manual
   add/edit, archive, merge.
2. **Bulk import** — CSV/XLSX parsing, column mapping, dedupe at scale,
   per-row failure reporting, and the upload UI.

## Data model

Tenant-scoped via the `TenantScoped` mixin (`app/db/base.py:33`). Every table
repeats `ENABLE` + `FORCE ROW LEVEL SECURITY` and the `tenant_isolation` policy
in its own migration — RLS is opt-in per table and `verify_rls_enforced()`
(`app/db/rls.py:58`) refuses to boot the app on a readable table that skips it.

Every foreign key between these tables is **composite**, carrying `tenant_id`
and referencing `(tenant_id, id)`. RLS does not filter foreign-key validation,
so without this a row in agency A can reference agency B's candidate and
Postgres accepts it. The client feature learned this; a test asserts the
cross-tenant insert fails.

### `candidates`

| Group | Columns |
|---|---|
| Identity | `full_name`, `email`, `phone_e164`, `phone_raw` |
| Role | `current_title`, `current_employer`, `location` |
| Placement | `years_experience`, `expected_salary`, `salary_currency`, `salary_period`, `available_from`, `notice_period_raw`, `employment_type` |
| Pipeline | `pipeline_stage` |
| Record | `record_status`, `merged_into_candidate_id` |
| Free text | `notes` |
| Audit | `created_by`, `updated_by`, `created_at`, `updated_at` |

**`pipeline_stage` and `record_status` are separate columns**, CHECK
constrained to `new | contacted | submitted | placed | rejected` and
`active | archived | merged`. They answer different questions — where the person
is in the process, and whether the row is still real. Collapsing them means
archiving someone destroys the fact that they were placed, which is the one
thing an agency most needs to keep.

**Phone is stored twice.** `phone_raw` is what the sheet said; `phone_e164` is
the normalised match key. This is the raw-beside-normalised rule `opportunities`
already follows: the raw string is what a recruiter recognises, the normalised
one is what a query matches on. `+65 9123 4567`, `6591234567` and `9123-4567`
are one number and only the canonical form can say so.

**`salary_period` is not optional decoration.** `opportunity.py:61` records why:
a figure without a period averages monthly and annual numbers into nonsense.

Unique per tenant, both partial `WHERE ... IS NOT NULL AND record_status <>
'merged'`:
- `(tenant_id, lower(email))`
- `(tenant_id, phone_e164)`

Excluding `merged` frees both keys for the surviving row, exactly as the client
domain index does. `archived` stays **inside** both indexes: an archived
candidate still holds their identity, and an import that skipped them would hit
the index on insert instead.

### `candidate_skills`

`(tenant_id, candidate_id, skill, skill_normalized)`, unique on
`(tenant_id, candidate_id, skill_normalized)`.

A table rather than an array column because skills are the field a recruiter
searches by, and a normalised row indexes in a way an array of free text does
not.

### `candidate_field_overrides`

`(tenant_id, candidate_id, field_name, human_value, changed_by, changed_at)`,
unique on `(tenant_id, candidate_id, field_name)`.

Mirrors `opportunity_field_overrides`. A field named here was edited by a
person, and an import may not overwrite it. Without this table the second upload
of an older export silently discards a recruiter's correction — and there is
nothing in the data afterwards that could tell it happened.

### `candidate_imports` and `candidate_import_rows`

The import run, recorded rather than merely performed.

- `candidate_imports`: `filename`, `uploaded_by`, `status`
  (`pending | running | complete | failed`), `row_count`, `created_count`,
  `updated_count`, `skipped_count`, `failed_count`, `column_mapping` (JSONB),
  `started_at`, `finished_at`, `error`.
- `candidate_import_rows`: `import_id`, `row_number`, `raw_values` (JSONB),
  `outcome` (`created | updated | skipped | failed`), `error`, `candidate_id`
  (nullable, composite FK).

A recruiter who uploads 500 rows and reads "31 failed" needs to know *which* 31
and why. Without the row table that is unanswerable, and the recruiter's only
recourse is to re-upload and hope.

## Matching

One service, `app/services/candidate_matching.py`. Given a set of incoming
values it resolves in order and stops at the first hit:

1. **Email**, lowercased.
2. **Phone**, normalised to E.164.
3. **No hit** — create.

Either key alone is sufficient. The common real case is a recruiter's older
sheet carrying a personal Gmail and the newer one a work address, with the
mobile unchanged; requiring both to agree would create a duplicate every time.

**Name is never a match key.** Two different people share a name far more often
than intuition suggests, and merging two real people's records is a materially
worse outcome than a duplicate a recruiter can merge later. This is the same
reasoning that kept `name_normalized` out of the client unique index.

### Phone normalisation

Parsed to E.164 using a configured default region — `settings.DEFAULT_PHONE_REGION`,
not a literal. A number that cannot be parsed confidently is stored in
`phone_raw` only, with `phone_e164` left NULL, and therefore never matches. A
half-parsed number used as an identity key is worse than none: it silently
splits or merges people.

**Non-mobile numbers never auto-match.** A Singapore office line (`6…`) is
shared by everyone at a company, so matching on it merges strangers. Mobile
prefixes come from settings alongside the region. Such a row creates a
candidate; the recruiter merges by hand if it is a duplicate.

### On conflict

The import writes a field only if no override exists for it. A protected field
is skipped; the incoming value remains readable in
`candidate_import_rows.raw_values`, which holds the whole row regardless.

`outcome` is per row, not per field: a row that changed at least one field is
`updated`, and a row whose every field was either identical or protected is
`skipped`. So a recruiter can tell "the import had nothing new for this person"
apart from "the import was overruled here" by reading the row's values against
the profile's overrides.

**Merge is human-only**, as with clients: it repoints skills and overrides to the
target, sets `record_status = 'merged'` with `merged_into_candidate_id` in the
same statement (a CHECK enforces both directions), and refuses to merge into an
already-merged row so chains cannot form.

## Import

`POST /api/candidates/imports` accepts the file; **the work runs on arq.**

The queue is already deployed and load-bearing (`tests/test_deployment.py` fails
if the `arq` service stops being deployed). A 500-row file doing two dedupe
lookups per row will outlive an HTTP timeout, and a half-finished import that
returns a 504 is the worst available outcome: rows are committed, the recruiter
sees an error, and nothing says how far it got. The UI polls the import's status
instead.

**Each row is validated and committed independently.** A malformed date fails
that row with a reason and the other 499 land. An import is not a transaction —
a partial success that reports itself accurately is more useful than an
all-or-nothing rollback of an hour's work.

Three cases the parser must handle because they are normal, not edge:

- **Duplicate rows within one file.** The second occurrence updates the record
  the first created, rather than colliding on the unique index.
- **Headers that are not our field names.** Hence the mapping step: the UI
  prefills a best guess and the recruiter confirms. The confirmed mapping is
  stored on the import so a failure can be explained afterwards.
- **Encoding.** Spreadsheets exported from Excel are frequently not UTF-8.
  Decode explicitly and fail the file with a clear message rather than writing
  mojibake into a person's name.

XLSX parsing needs a new dependency (`openpyxl`). File size and row caps come
from settings.

## API

Under `/api`, following `app/api/opportunities.py` and `app/api/clients.py`:
`_require_session` → `tenant_session` → `limit`/`offset` clamped rather than
rejected from `settings.CANDIDATES_PAGE_LIMIT`, response
`{items, total, limit, offset, counts}` with counts computed over the whole
tenant so they do not shift while paging.

- `GET /api/candidates` — `pipeline_stage` filter, and search across name,
  email and phone. Excludes `merged` by default.
- `GET /api/candidates/{id}` — includes skills and which fields carry overrides
- `POST /api/candidates`, `PATCH /api/candidates/{id}` — a PATCH records an
  override for every field it changes
- `POST /api/candidates/{id}/archive`, `/merge`, `/unmerge`
- `POST /api/candidates/imports`, `GET /api/candidates/imports/{id}`
- `DELETE /api/candidates/{id}` and `GET /api/candidates/{id}/export`

Another agency's id returns **404, never 403**. Confirming that an id exists but
belongs to someone else is itself a cross-tenant disclosure.

## UI

At `/dashboard/candidates`, reusing the existing dashboard components rather
than introducing a parallel set — `job-orders-table.tsx` and `detail-panel.tsx`
already establish the table, panel and pagination patterns.

- **List** — pipeline-stage chips, search, pagination
- **Detail panel** — full record, skills, edit in place; an overridden field is
  marked so it is clear why an import did not change it
- **Add / edit form** — name required; everything else optional, because a
  recruiter often has a name and a phone number and nothing more
- **Import** — four steps: upload, map columns, preview the first rows, then a
  summary with the failed rows and their reasons

## Personal data

Candidates are the first genuinely personal records this system holds. Two
endpoints ship with the feature rather than after it: **hard delete** and
**single-candidate export**.

**Deleting a candidate must also scrub `candidate_import_rows`.** Those rows
hold the original spreadsheet values — name, email, phone. A delete that clears
the profile and leaves them satisfies the UI and not the law. The delete removes
the candidate, its skills, its overrides, and nulls or purges the personal
values in every import row that produced it, keeping the row itself so the
import's counts still reconcile.

**Notes are personal data with no structure.** They ship because a recruiter
needs them, but they are covered by delete and export like every other field.

### What this does not build

**The retention purge worker.** `mailboxes.retention_months` and
`email_messages.retention_until` are written on every message
(`app/workers/jobs.py:935`) and **nothing reads them** — there is no purge job
anywhere in the codebase, still true after the notification system merged. That
worker is separate work covering email bodies as well as candidates. Recording
it here so its absence is not mistaken for an oversight: candidate deletion is
manual until it exists.

## Testing

Against the throwaway local Postgres 16, never Koyeb — `tests/conftest.py:44`
aborts collection on a non-local host.

- **Isolation.** Agency A cannot read, update, or FK-reference agency B's
  candidates, skills, overrides, or import rows. The FK test is written first
  and must fail before the composite keys exist.
- **Matching.** Email match; phone match; either alone is sufficient; an
  unparseable phone never matches; an office-line number never auto-matches; a
  shared name never matches.
- **Conflict.** An import updates an untouched field; an import does not
  overwrite an overridden field; the incoming value is still recoverable
  afterwards.
- **Import.** A malformed row fails alone and the rest land; duplicate rows
  within one file resolve to one candidate; a non-UTF-8 file fails with a
  message naming the encoding; the counts on the import equal the outcomes on
  its rows.
- **Merge.** Skills and overrides repoint, both unique keys free up, chains are
  refused, the `record_status`/`merged_into` CHECK holds.
- **Personal data.** Deleting a candidate leaves no name, email or phone in
  `candidate_import_rows`.
- **Routing.** `tests/test_routing.py` already fails if a route escapes `/api`.
