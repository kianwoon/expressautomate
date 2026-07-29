# Candidate and work-history import

Decided 2026-07-29. Backend and UI. Piece 4 of
[work history and sourcing](2026-07-28-candidate-sourcing-decomposition.md).

An agency arriving on this platform has a spreadsheet, not five hundred CVs.
This is the path that gets their existing list in on day one.

## The decomposition was wrong about this piece

It assumed piece 4 would add history rows to a candidate import that already
existed, because
[candidate profiles](2026-07-28-candidate-profiles-design.md) describes a
CSV/XLSX upload. That is Phase 2 of that spec, and **only Phase 1 was built** —
manual add and edit. There is no import endpoint anywhere in the codebase, and
no library for reading tabular files.

So this piece builds the candidate import and the history import together.
They share the upload, the parsing, the matching, the job and the undo;
building them separately would build all of that twice.

What is real and reusable, rather than merely written down:

- `CandidateFieldOverride` is implemented — written at
  `app/api/candidates.py:696`, read at `:313` and `app/api/candidate_roles.py:207`.
  The rule *the import wins, except on a field a human edited* has working
  machinery behind it.
- `candidate_roles.source` already accepts `"import"`.
- Piece 2's upload endpoint, size cap, byte sniffing, arq job and bounded zip
  inflate all transfer directly.

## Decisions

| Question | Answer |
|---|---|
| Scope | One importer covering candidates **and** history |
| Formats | CSV and XLSX |
| How a history row names its candidate | Its own email or phone column, matched like any candidate |
| Bad rows | Collected and reported; the run continues |
| Preview | **No** — valid rows are written immediately |
| Re-import | Matched and updated, never duplicated |
| Imported row status | `confirmed` — a spreadsheet is a person's record, not a proposal |

## Undo, because there is no preview

Writing straight to live data with no dry run is only defensible if it can be
taken back. A mis-mapped column silently rewrites hundreds of rows, and that is
the failure a preview would have caught.

Undo has two halves, because they are different problems:

1. **Rows the import created** carry an `import_id`. Undo deletes them.
2. **Fields the import overwrote** have their previous value recorded before
   the write. Undo restores them.

Without the second half, undo would delete new candidates while leaving a
hundred rewritten phone numbers in place — worse than no undo at all, because
it would look complete.

Undo refuses while an import is still parsing, and is safe to call twice: a
second attempt finds nothing to reverse rather than trampling edits made since.

### Where the record lives

**`candidate_import_changes`**, `TenantScoped`, one row per thing the import
touched:

| Column | Notes |
|---|---|
| `import_id` | The import that made the change |
| `entity_type`, `entity_id` | `candidate` or `candidate_role`, and which one |
| `action` | `created` or `updated` |
| `field_name`, `previous_value` | Null on a `created` row — there was nothing before |
| `new_value` | What the import wrote. Without it the restore rule below cannot be evaluated at all |

A created row also carries its `import_id` directly on `candidates` /
`candidate_roles`, so the common case — undo everything this import made —
needs no join.

The cost is small: five hundred candidates with a few changed fields each is a
few thousand narrow rows, written once and read only by an undo.

### The restore rule, which is what makes undo safe

**A field is restored only if its current value still equals what the import
wrote** — which is why `new_value` is recorded alongside `previous_value`. With
only the previous value there is nothing to compare against, and the rule
cannot be evaluated: a field now holding something a recruiter typed is
indistinguishable from one still holding what the import put there.

If a recruiter has since corrected it by hand, the import's undo has no
business reaching in — their edit is newer and better, and reverting it would
be the import damaging exactly the data a person cared enough to fix.

That single rule is what makes the two promises above true: calling undo twice
is harmless because the second pass finds nothing matching, and edits made
after the import survive it.

Skipped fields are counted and named in the report, so an undo that reversed
less than the whole import says so rather than implying a clean reversal.

## Storage

**`candidate_imports`**, inheriting `TenantScoped`, shaped like
`candidate_documents`: `filename`, `content_type`, `byte_size`, `object_key`,
a state of `pending` | `parsing` | `done` | `failed` | `undone`, per-outcome counts
(candidates created and updated, roles created and updated, rows failed),
`error_report_key`, `uploaded_by` and timestamps.

The error report is a file in R2 beside the upload, not a column. A
five-hundred-row migration can produce hundreds of problems, and the same
reasoning applies that kept CV text out of Postgres.

Both keys begin `{tenant_id}/`, the prefix a tenant erasure sweep purges by.

## Reading the file

**The type comes from the bytes.** XLSX is a zip, so `PK` proves nothing on its
own — it is XLSX only if the archive contains `xl/workbook.xml`, exactly the
test DOCX needed. CSV has no magic number and is the fallback once the file
decodes as text.

**XLSX therefore goes through piece 2's bounded inflate**
(`_inflate_bounded` and `_bounded_docx_archive`, `app/services/cv/text.py:162`
and `:235`). A spreadsheet is a decompression-bomb vector in precisely the way
a DOCX is, and that code already refuses to trust a member's declared
uncompressed size. Handing bytes straight to `openpyxl` would reopen a hole
that took two review rounds to close.

Those helpers repack to a `ZIP_STORED` archive, which `openpyxl` reads as
happily as `python-docx` does. They need generalising out of the DOCX-specific
names rather than copying — a second bounded inflate is a second thing to get
wrong, and the wrong one will be the one nobody reviewed.

**Columns are named, never guessed** — fixed headers matched
case-insensitively, with a downloadable template. Positional guessing is how a
mis-mapped column writes hundreds of wrong rows, which is the failure this
design is already paying for with undo.

An XLSX carries a `Candidates` sheet and a `History` sheet. CSV users send one
or two files. A history row names its candidate by repeating that person's
email or phone.

`openpyxl` is a new dependency. CSV is standard library.

## Matching

Nothing new is invented here; both rules already exist and are tested.

- **A candidate** matches on **email or phone, either alone** — `find_candidate`
  in `app/services/candidate_matching.py`, the same identity resolution the
  manual path uses. Requiring both to agree would duplicate a person every time
  an old sheet carried a personal address and a new one a work address.
- **A history row** matches on **employer plus overlapping dates** — the rule
  piece 2 built, including its corrections: months pinned before comparison,
  half-open overlap so adjacent roles stay two roles, and live roles consulted
  before rejected ones.

On a match the row is updated, **except on any field a human edited**. Only
genuinely new rows are created.

### When email and phone name two different people

`find_candidate` can return a conflict: the row's email belongs to one
candidate and its phone to another. That is not a match to be resolved by
preferring one key — it is a spreadsheet saying two contradictory things about
who this is, and picking a side would merge or split two real people.

**A conflict is a reported bad row.** Nothing is written for it, the report
names both candidates it collided with, and the recruiter decides. This is the
same instinct the manual path already has: merging two real people is worse
than a duplicate somebody can merge later.

### What a real export actually contains

A spreadsheet exported from an agency's old system is not clean, and every one
of these appears:

- **Empty trailing rows.** Skipped silently. They are an artefact of how the
  file was saved, not a problem a recruiter should be shown five hundred times.
- **Merged cells** read as empty in every row but the first. Treated as empty,
  which the rules below already handle.
- **The same candidate twice in one file.** The second row is applied to the
  same person, not a second one — the run keeps what it has already matched or
  created, so a sheet listing somebody twice does not produce two of them.
- **A history row naming nobody in the Candidates sheet.** It is matched
  against candidates already in the system; if that finds nothing, it is a
  reported problem, not a new person. A history row carries no name, so
  inventing a candidate from one would create a record with a job and no human
  attached to it.
- **Candidates are applied before history**, in one run, so a history row can
  attach to a person created moments earlier in the same file. Any other order
  makes a single-file migration impossible.

### Why imported rows are confirmed

`unconfirmed` means a model proposed something and no person has looked at it.
A spreadsheet is the agency's own record, written by people. Importing five
hundred rows into a review queue nobody asked for would misuse the state and
bury the CV proposals that actually need a decision. `source` stays `import`,
so provenance is still exact.

### Dates keep the precision the cell had

A cell reading "Mar 2019" is month precision; a real date cell is day
precision. No component is invented — the same §15 rule the CV parser follows,
and the reason `started_precision` exists at all.

## API

A new file, `app/api/candidate_imports.py`.

- `POST /api/candidates/imports` — the upload, answering **202**.
- `GET /api/candidates/imports` — recent imports with state and counts, so a
  migration is visible while it runs.
- `GET /api/candidates/imports/{id}/errors` — a short-TTL presigned URL to the
  report.
- `GET /api/candidates/imports/template` — the file with the headers we accept,
  because the alternative is asking an agency to guess.
- `POST /api/candidates/imports/{id}/undo`.

Another agency's import is **404, never 403**. Row and file caps come from
`settings`, and the job carries a timeout. The upload enqueues its job and, on
a `False` return from `enqueue()`, marks the row `failed` with a retryable
message rather than leaving it `pending` for ever — the hole piece 2 found.

Imports stranded in `pending` or `parsing` join `rescan_stuck`
(`app/workers/tasks.py:111`) the way `candidate_documents` already does — a
second query and enqueue block inside that function, since it is welded to
email rows and cannot simply be pointed at a new table.

## UI

On the candidates page, not the detail panel: this is a bulk action on the
list. A recent-imports table shows state and counts while a migration runs, and
is where a recruiter reaches the error report and the undo.

Undo asks for confirmation and says what it will reverse, in counts.

## Tests

1. Agency A cannot see, download the errors of, or undo Agency B's import.
2. An XLSX crafted as a decompression bomb is refused.
3. Wrong type → 415; oversized → 413; too many rows → refused, naming the cap.
4. A bad row is collected with its sheet and line number, and the run continues.
5. A candidate matches on email alone; another on phone alone.
6. A history row matching an existing role updates it and creates no duplicate.
7. The import wins on an ordinary field and **loses** on one a human edited.
8. Re-uploading the same file changes nothing and duplicates nothing.
9. Undo deletes what the import created **and restores what it overwrote**.
10. Undo twice is harmless.
11. A field a recruiter corrected **after** the import is not reverted by undo,
    and the report says how many were skipped and why.
12. A row whose email and phone name two different candidates is reported as a
    conflict, writes nothing, and names both.
13. The same candidate listed twice in one file produces one person, not two.
14. A history row naming nobody is reported, and no candidate is invented for it.
15. Empty trailing rows are skipped silently, not reported.
16. "Mar 2019" imports as month precision, with no day.
17. RLS is enforced on every new table, including `candidate_import_changes`.

## Out of scope

CVs arriving as email attachments (piece 3) and ranking candidates against a
job order (piece 5). Importing clients, opportunities or placements — this
piece is candidates and their history only.
