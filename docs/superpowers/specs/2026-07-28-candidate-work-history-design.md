# Candidate work history

Decided 2026-07-28. Backend and UI. Piece 1 of
[work history and sourcing](2026-07-28-candidate-sourcing-decomposition.md).

A candidate is currently one flat row: a current title, a current employer, and
`years_experience` as a single integer (`app/models/candidate.py:40-136`). This
adds the level beneath it — the roles a person actually held, with dates — so
that sourcing has something to reason about, and so a recruiter reading a
record can see a career instead of a job title.

Only a recruiter writes these rows in this piece. Nothing is parsed, uploaded
or extracted here; those are pieces 2 to 4.

## Decisions

| Question | Answer |
|---|---|
| What one entry records | Employer, title, dates, employment type, location, free-text description |
| Date precision | Recorded as `year`, `month` or `day` — never invented |
| Where rows come from, eventually | Typing, CV upload, email attachments, spreadsheet import |
| When a parsed row contradicts a typed one | It arrives `unconfirmed` and waits for a person |
| `current_title`, `current_employer`, `years_experience` | Derived from history when history exists; columns stay |
| Per-role skills | No — `CandidateSkill` stays candidate-level |

## The table

`candidate_roles`, inheriting `TenantScoped` (`app/db/base.py:33`), with a
composite `(tenant_id, candidate_id)` foreign key following the idiom already
used at `app/models/candidate.py:111-116`.

| Column | Notes |
|---|---|
| `employer`, `employer_normalized` | The normalized twin is what piece 5 will match on |
| `title`, `title_normalized` | Same |
| `started_on`, `ended_on` | `ended_on IS NULL` means the role is current |
| `started_precision`, `ended_precision` | `year` \| `month` \| `day` |
| `employment_type`, `location` | The structured core |
| `description` | Free text; usually empty |
| `source` | `human` \| `cv_upload` \| `email_attachment` \| `import` |
| `status` | `unconfirmed` \| `confirmed` \| `rejected` |
| `extraction_id` | Nullable FK to `Extraction`, for rows a model produced |
| `created_by`, `updated_by`, timestamps | As elsewhere |

**Concurrent roles are allowed.** No constraint forces a single open-ended row,
because people genuinely hold two jobs at once. "Current" means the open-ended
role with the latest `started_on`.

### Why precision is a column

A CV that says "Mar 2019" does not say the day. Storing `2019-03-01` and
rendering "1 March 2019" fabricates a fact the source never stated, which §15
forbids. Precision lets the UI say "Mar 2019" honestly while date arithmetic
still works.

### Why `source` and `status` ship now

Piece 1 only ever writes `human` and `confirmed`. Both columns exist anyway,
because adding them once rows are live means a migration over every tenant's
data. The cost today is two columns with one value each.

## Derivation

The scalar fields stay, and become a cache. The list table, search, the import
spec and the existing tests all read them, and none of that has to change.

Recomputation happens in the application layer after any role mutation, in the
same transaction. Not a database trigger: a value that changes with no visible
cause is the kind of thing nobody can debug at 6pm.

**`current_title` and `current_employer`** come from the open-ended role with
the latest `started_on`. When no role is open-ended, they come from the most
recently ended one and the panel says "Most recently" rather than "Current" — a
candidate between jobs has no current employer, and the screen should not
imply otherwise.

**`years_experience`** is the **union** of role spans in months, floored to
years. Union rather than sum: two concurrent jobs through 2020 are one year of
experience, not two. Gaps are not counted. An open-ended role counts to today,
so the value drifts, and is therefore recomputed when the detail record is
read rather than only when it is written.

**Year-only precision counts from mid-year.** A role recorded as "2019–2021" is
somewhere between one and three years; taking July avoids a systematic bias in
either direction. Refusing to compute would make the field useless for exactly
those candidates whose history came from a vague CV.

**A human override wins.** `CandidateFieldOverride` already means "a person
asserted this, do not let an import overwrite it"
(`app/api/candidates.py`). Derivation honours it: if `years_experience` is in a
candidate's override set, the computed value is discarded. No new mechanism,
and the same promise the import path already makes.

**Deleting the last role nulls the columns.** `current_title`,
`current_employer` and `years_experience` were derived from the very role the
recruiter just removed. If they deleted it because it was mistyped, keeping
the stale value preserves precisely the wrong data — and with no
`CandidateFieldOverride` recorded for it, that value is indistinguishable
from derived truth. §15 forbids asserting a fact no source states, so a
candidate with no remaining non-rejected role must not keep displaying one.
Any of the three fields that does carry an override is left alone, exactly as
it would be during ordinary re-derivation — the override is a person's own
assertion, not derivation's, in both directions.

## API

A new file, `app/api/candidate_roles.py`. `candidates.py` is 823 lines against
a 1500-line ceiling, and this does not go in it.

- `POST /api/candidates/{id}/roles`
- `PATCH /api/candidates/{id}/roles/{role_id}`
- `DELETE /api/candidates/{id}/roles/{role_id}`

Roles are returned embedded in the existing candidate GET. The panel already
fetches that record; a second round trip to draw one section earns nothing.

Every route resolves the candidate through the tenant-scoped `_load()` in
`candidates.py:209`. A candidate in another tenant is a **404, not a 403** —
the same choice the avatar endpoints make, and for the same reason: existence
is not leaked.

Validation answers with 422 and a real sentence. The frontend's `readError`
(`frontend/app/dashboard/candidates.ts:185`) surfaces `detail`, so a recruiter
reads "A role cannot end before it starts" instead of a generic failure.

The migration adds the table and its `tenant_isolation` policy in one revision,
following `20260726_1800_row_level_security.py`. `verify_rls_enforced()`
(`app/db/rls.py:58`) refuses to boot without the policy, so a forgotten one is
a failed deploy rather than a silent leak.

## UI

A new component, `frontend/app/dashboard/candidates/candidate-history.tsx`,
rendered in the detail panel below the field rows. `candidate-panel.tsx` is
already past 450 lines and this does not go inside it.

The list is a timeline, newest first, current roles pinned above the rest.
Employer and title on one line; the date range and computed duration beneath;
then employment type and location. The description sits behind a disclosure,
because most roles will not have one and six empty boxes read as a bug.

**Dates render at the precision they were recorded at** — "Mar 2019 – Jun 2021
· 2 yr 4 mo", never "1 March 2019". The input is a month/year picker. Asking a
recruiter for a day they do not have is how fabricated data gets in.

**Adding and editing happen inline**, not in a dialog stacked over the panel.

**Unconfirmed rows are styled now**, with a left border and confirm/reject
actions, even though nothing writes them until piece 2. Building the state now
means piece 2 is a parser and an endpoint rather than a parser and a redesign.

### The stylesheet has to be split first

`frontend/app/globals.css` is at 1496 lines against the 1500-line ceiling.
This section's styles do not fit. Dashboard styles (`.jo-*`, `.ca-*`, and the
new `.ch-*`) move to their own sheet; landing-page styles stay. Mechanical,
but real work, and it belongs in the plan rather than being discovered halfway
through the build.

## Tests

1. Agency A cannot read, write or delete Agency B's roles.
2. Overlapping roles count once — the union rule, which is the thing most
   likely to be written as a sum.
3. Gaps are excluded; an open-ended role counts to today; year precision counts
   from mid-year.
4. A `years_experience` override survives derivation.
5. Deleting the last role nulls the cached columns, except any that carry an
   override.
6. `ended_on` before `started_on` is a 422 carrying a readable message.
7. Roles order current-first, then newest by `started_on`.
8. RLS is enforced on the new table — `verify_rls_enforced()` covers this, and
   the test suite must not be able to pass without the policy.

## Out of scope

CV parsing, attachment storage, spreadsheet import of history rows, and any
ranking of candidates against a job order. Each has its own piece in the
[decomposition](2026-07-28-candidate-sourcing-decomposition.md).
