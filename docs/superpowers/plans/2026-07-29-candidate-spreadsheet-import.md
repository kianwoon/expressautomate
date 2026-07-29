# Candidate and Work-History Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agency uploads its existing candidate list and work history as a spreadsheet, and it lands — matched against what is already there, with every bad row reported and the whole import reversible.

**Architecture:** The upload is stored in R2 and recorded in `candidate_imports`. An arq job reads the file through the bounded zip inflate built for CVs, parses rows against named columns, matches candidates with `find_candidate` and roles with `match_existing_role`, writes what is valid while recording every change in `candidate_import_changes`, and files an error report in R2. Undo replays those changes backwards.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Alembic, Postgres 16 with RLS, arq on Redis, openpyxl, pytest, Next.js static export.

## Global Constraints

- All config from the repo-root `.env` via `app.core.config.settings`. **No hardcoded limits, caps, quotas or TTLs.**
- Every business table carries `tenant_id` via `TenantScoped` (`app/db/base.py:33`), with `ENABLE`/`FORCE ROW LEVEL SECURITY` and a `tenant_isolation` policy in the **same** migration. `verify_rls_enforced()` (`app/db/rls.py:58`) refuses to boot otherwise.
- Every route under `/api`; routers mount on an `api` router that already carries the prefix — do **not** pass `prefix="/api"` again.
- Another agency's row is **404, never 403**.
- Every arq job carries `tenant_id` in its payload.
- **No source file over 1500 lines.** Current: `candidates.py` 997, `candidate_roles.py` 437, `models/candidate.py` 376, `tasks.py` 399, `config.py` 654, `cv/text.py` 296, `cv/persist.py` 625, `app.css` 941, `candidates/page.tsx` 402.
- **§15 — never assert a fact no source states.** No date component may be invented; a cell reading "Mar 2019" is month precision.

**Running the backend suite** — use this and nothing else:

```bash
cd backend && scripts/test-env.sh -q
```

It sources `backend/.env.test` and hides any root `.env`. **Do not hand-roll environment variables and do not copy CI's** — CI uses a different application-role password than the local database, and forcing it produces hundreds of bogus authentication failures that look like flakiness. Baseline: **989 passed, 1 skipped**. Also `uv run ruff check .`. Alembic head: `e2f7b8c15a44` (`20260729_1200_cv_parse_outcome.py`).

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/models/candidate.py` (modify) | `CandidateImport`, `CandidateImportChange`; `import_id` on `Candidate` and `CandidateRole` |
| `backend/alembic/versions/20260729_1300_candidate_imports.py` (create) | Both tables, both columns, RLS |
| `backend/app/services/archive.py` (create) | The bounded zip inflate, generalised out of `cv/text.py` |
| `backend/app/services/imports/table.py` (create) | Bytes → rows. Sniffing, CSV, XLSX. Pure. |
| `backend/app/services/imports/rows.py` (create) | Rows → typed records, with per-row problems. Pure. |
| `backend/app/services/imports/apply.py` (create) | Matching, writing, change recording |
| `backend/app/services/imports/undo.py` (create) | Replaying changes backwards |
| `backend/app/workers/import_jobs.py` (create) | `run_candidate_import`. **Not** `jobs.py` |
| `backend/app/workers/settings.py`, `tasks.py` (modify) | Register the job; join the stuck sweep |
| `backend/app/api/candidate_imports.py` (create) | Upload, list, errors, template, undo |
| `backend/app/core/config.py` (modify) | Import settings |
| `frontend/app/dashboard/candidates/candidate-imports.tsx` (create) | Upload, recent imports, undo |
| `frontend/app/dashboard/candidates/page.tsx`, `candidates.ts`, `api.ts`, `app.css` (modify) | Mount, types, paths, styles |

---

### Task 1: The tables, and the trail undo walks back

**Files:**
- Modify: `backend/app/models/candidate.py` (append after `CandidateDocument`, which ends at `:376`)
- Create: `backend/alembic/versions/20260729_1300_candidate_imports.py`
- Test: `backend/tests/test_candidate_imports.py`

**Interfaces produced:**
- `CandidateImport` — `filename`, `content_type`, `byte_size`, `object_key`, `state`, `error_report_key`, `candidates_created`, `candidates_updated`, `roles_created`, `roles_updated`, `rows_failed`, `uploaded_by`. Constants `PENDING`, `PARSING`, `DONE`, `FAILED`, `UNDONE`, `IMPORT_STATES`.
- `CandidateImportChange` — `import_id`, `entity_type`, `entity_id`, `action`, `field_name`, `previous_value`. Constants `CANDIDATE`, `ROLE`, `CREATED`, `UPDATED`.
- `Candidate.import_id` and `CandidateRole.import_id`, both nullable.

Model these on `CandidateDocument` (`models/candidate.py:300-376`) — same mixins, same composite-FK idiom, same constant style. Read it before writing.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_candidate_imports.py`. Use the suite's real fixtures — `agency`, `other_agency`, `_a_candidate_row` from `tests/test_candidate_roles_api.py`, `AdminSessionLocal` from `tests.conftest`, `tenant_session` from `app.db.rls`. There is no `client` or `candidate_factory` fixture.

**`pytest.raises` must wrap the whole `async with tenant_session(...)` block**, not sit inside it — the context manager commits again on exit, and in an aborted transaction that raises `PendingRollbackError` outside your `raises`. Two earlier tasks learned this.

```python
"""An import, and the trail that lets it be taken back."""

import pytest
from sqlalchemy import select

from app.models.candidate import CandidateImport


@pytest.mark.asyncio
async def test_an_import_belongs_to_one_tenant_only(agency, other_agency):
    """Agency B cannot see Agency A's import even knowing its id."""
    a_tenant, a_user = agency
    b_tenant, _b_user = other_agency

    async with tenant_session(a_tenant) as session:
        session.add(
            CandidateImport(
                tenant_id=a_tenant,
                filename="roster.xlsx",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                byte_size=1024,
                object_key=f"{a_tenant}/imports/roster.xlsx",
                state=CandidateImport.PENDING,
                uploaded_by=a_user,
            )
        )
        await session.commit()

    async with tenant_session(b_tenant) as session:
        assert (await session.execute(select(CandidateImport))).scalars().all() == []
```

Add: the same isolation test for `CandidateImportChange`; and that `state` rejects a value outside `IMPORT_STATES`.

- [ ] **Step 2: Run it, watch it fail**

Expected: `ImportError: cannot import name 'CandidateImport'`.

- [ ] **Step 3: Models**

`CandidateImportChange` carries the reasoning in its docstring: undo has two halves because deleting created rows while leaving overwritten fields in place would look complete and not be. `previous_value` is `Text` and nullable — null on a `created` row, because there was nothing before.

`import_id` on `Candidate` and `CandidateRole` is nullable with **no** foreign key action that would cascade a delete of the import into the data it made: an import is a record of an event, and deleting that record must not delete a person. Use `ondelete="SET NULL"` and say why in a comment.

- [ ] **Step 4: Migration**

Both tables with RLS in the same revision — copy the predicate from `20260729_1100_candidate_documents.py`, which copied it from `20260726_1800_row_level_security.py:93-102`. Grant DML with `settings.DATABASE_APP_ROLE`, never a literal. `down_revision = "e2f7b8c15a44"`.

- [ ] **Step 5: Verify and commit**

`scripts/test-env.sh -q`, `uv run ruff check .`, and `uv run alembic check` (must report no new upgrade operations). Never migrate the production Koyeb database.

```bash
git commit -m "Record an import, and what it would have to undo"
```

---

### Task 2: One bounded inflate, shared

**Files:**
- Create: `backend/app/services/archive.py`
- Modify: `backend/app/services/cv/text.py` (delegate to it), `backend/pyproject.toml` (add `openpyxl`)
- Test: `backend/tests/test_archive.py`

**Why this task exists.** `cv/text.py` already refuses to trust a zip member's declared uncompressed size — it inflates each member itself against a budget and repacks to `ZIP_STORED`. That took two review rounds to get right, after a first attempt trusted the central directory and a reverse bomb walked through it. XLSX is a zip too. Copying that code would create a second one to get wrong, and the wrong one would be the one nobody reviewed.

**Interfaces produced:**
- `inflate_bounded(data: bytes, info: zipfile.ZipInfo, budget: int) -> bytes`
- `bounded_archive(data: bytes, *, budget: int) -> io.BytesIO`
- `archive_contains(data: bytes, member: str) -> bool`
- `BoundedArchiveTooLarge(Exception)`

**Interfaces consumed:** the existing `_inflate_bounded` (`cv/text.py:162`) and `_bounded_docx_archive` (`:235`) — move them, do not reimplement. `cv/text.py` then calls the shared versions.

- [ ] **Step 1: Write the failing test**

Move the existing bomb tests from `tests/test_cv_text.py` that exercise the inflate itself, and add one for XLSX: an archive containing `xl/workbook.xml` whose member declares a small `file_size` but genuinely inflates past the budget is refused. Build it with `zipfile` in the test — do not commit a binary.

- [ ] **Step 2: Move, do not rewrite**

Lift both functions into `app/services/archive.py` with public names, drop the DOCX-specific naming, and have `cv/text.py` import them. **The CV tests must still pass unchanged** — that is the proof the move was faithful.

- [ ] **Step 3: Add `openpyxl`**

`uv add openpyxl` (or edit `pyproject.toml` and `uv sync`). Nothing else in the repo reads tabular files.

- [ ] **Step 4: Verify and commit**

```bash
git commit -m "Share one bounded inflate, rather than write a second"
```

---

### Task 3: Bytes to rows

**Files:**
- Create: `backend/app/services/imports/__init__.py`, `backend/app/services/imports/table.py`
- Test: `backend/tests/test_import_table.py`

**Interfaces produced:**
- `sniff_table(data: bytes) -> "csv" | "xlsx" | None` — XLSX only if the archive contains `xl/workbook.xml`; CSV is the fallback once the bytes decode as text.
- `read_sheets(data: bytes, kind: str, *, budget: int, max_rows: int) -> dict[str, list[dict[str, str]]]` — sheet name to rows, each row a dict keyed by **lower-cased, stripped** header.
- `TooManyRows(Exception)`, `UnreadableTable(Exception)`

**Rules:** headers are matched case-insensitively and never positionally. An XLSX carries `Candidates` and `History` sheets; a CSV is a single sheet whose name the caller supplies. XLSX goes through `bounded_archive` from Task 2 before `openpyxl` sees it.

- [ ] **Step 1: Write the failing tests**

- A CSV with a header row and two data rows yields two dicts keyed by header.
- Headers differing only in case and surrounding spaces match.
- A bare zip without `xl/workbook.xml` is **not** XLSX.
- A file that is neither returns `None` from `sniff_table`.
- Past `max_rows`, `TooManyRows` names the cap.
- A cell containing only whitespace reads as empty, not as `" "`.

- [ ] **Step 2: Implement, then verify and commit**

```bash
git commit -m "Read a spreadsheet by what it is, and by the names in its header"
```

---

### Task 4: Rows to records, and every problem with it

**Files:**
- Create: `backend/app/services/imports/rows.py`
- Test: `backend/tests/test_import_rows.py`

**Interfaces produced:**
- `CandidateRecord` and `RoleRecord` dataclasses.
- `RowProblem` — `sheet`, `line`, `reason`. `line` is the **spreadsheet's own line number**, header included, because that is what the recruiter sees.
- `parse_candidates(rows) -> tuple[list[CandidateRecord], list[RowProblem]]` and `parse_roles(rows) -> tuple[list[RoleRecord], list[RowProblem]]`

**Rules that matter:**
- Reuse `normalize_email` and `normalize_phone` from `app/services/candidate_naming.py`. A phone that will not normalise is a `RowProblem`, not a silent null.
- **Dates keep the precision the cell had.** `2019` is year precision, `Mar 2019` month, a real date cell day. Never invent a component — this is §15, and `started_precision` exists for it.
- A row with neither email nor phone is a problem: nothing can match it to a person.
- A parse never raises. Every failure becomes a `RowProblem` and the run continues.

- [ ] **Step 1: Write the failing tests**

One per rule above, plus: a role row missing both employer and title is a problem; `Mar 2019` yields month precision with no day; an Excel date cell yields day precision.

- [ ] **Step 2: Implement, then verify and commit**

```bash
git commit -m "Turn a row into a record, or into a sentence a recruiter can act on"
```

---

### Task 5: Matching, writing, and remembering what changed

**Files:**
- Create: `backend/app/services/imports/apply.py`, `backend/app/services/imports/undo.py`
- Test: `backend/tests/test_import_apply.py`, `backend/tests/test_import_undo.py`

**Interfaces consumed — reuse these, do not write new rules:**
- `find_candidate(session, tenant_id, email, phone_e164) -> MatchResult` (`app/services/candidate_matching.py:58`). `MatchResult` has three branches: a `candidate_id` with `matched_on`; a `conflict` tuple of `(email_id, phone_id)`; or neither.
- `match_existing_role(employer_normalized, started_on, started_precision, ended_on, ended_precision, existing, today) -> uuid.UUID | None` (`app/services/cv/persist.py:255`).
- `overridden_fields(...)` (`app/api/candidate_roles.py:202`) — the set of fields a human edited.

**Interfaces produced:**
- `apply_import(session, *, tenant_id, import_id, candidates, roles, today) -> ImportOutcome` with counts and problems.
- `undo_import(session, *, tenant_id, import_id) -> UndoOutcome` with counts restored, deleted and skipped.

**The rules, exactly:**
1. A `conflict` from `find_candidate` is a **`RowProblem`, never a match.** Nothing is written; the report names both candidates. Picking a side would merge or split two real people.
2. On a candidate match, **the import wins except on a field in `overridden_fields`.** Every field actually changed gets a `CandidateImportChange` with its previous value, written **before** the update.
3. A created row carries `import_id` and gets a `created` change row.
4. A role matches via `match_existing_role`; a match updates, no match creates. Imported rows are `source="import"`, `status="confirmed"` — a spreadsheet is a person's own record, not a proposal.
5. **Undo restores a field only if its current value still equals what the import wrote.** A recruiter's later correction is newer and better; reverting it would damage the data somebody cared enough to fix. Skipped fields are counted and reported. This rule is also what makes undo safe to call twice.

- [ ] **Step 1: Write the failing tests**

Every rule above, plus: re-applying the same records changes nothing and duplicates nothing; undo twice is harmless; undo deletes created rows **and** restores updated fields.

- [ ] **Step 2: Implement, then verify and commit**

```bash
git commit -m "Let the sheet win, except where a person already spoke"
```

---

### Task 6: The job, the routes, and the way back

**Files:**
- Create: `backend/app/workers/import_jobs.py`, `backend/app/api/candidate_imports.py`
- Modify: `backend/app/workers/settings.py`, `backend/app/workers/tasks.py`, `backend/app/main.py`, `backend/app/core/config.py`
- Test: `backend/tests/test_import_job.py`, `backend/tests/test_candidate_imports_api.py`

**The job goes in `import_jobs.py`, not `jobs.py`** — that file is at 1443 lines against a 1500 ceiling. Model `run_candidate_import(ctx, *, tenant_id, import_id)` on `parse_candidate_cv` (`app/workers/cv_jobs.py:78`): the same state guards, the same move to a working state before the long operation. Register it in `settings.py`'s `functions` list **with a timeout from settings**, as `parse_candidate_cv` is.

**Join the stuck sweep.** `rescan_stuck` (`app/workers/tasks.py:111`) already has a second query/enqueue block for `candidate_documents` at `:150-161`. Add a third for imports stranded in `pending` or `parsing`, and include them in the count it returns.

**Routes**, in `app/api/candidate_imports.py`, following `candidate_documents.py:172-262` for ordering and the size cap (`_read_within_limit` at `:140`) — but using `sniff_table` from Task 3, not the CV sniff:

- `POST /api/candidates/imports` → **202**. On a `False` from `enqueue()` — it returns a bool and never raises — the row goes to `failed` with a retryable message.
- `GET /api/candidates/imports` — recent imports with state and counts.
- `GET /api/candidates/imports/{id}/errors` — short-TTL presigned URL.
- `GET /api/candidates/imports/template` — the headers we accept.
- `POST /api/candidates/imports/{id}/undo` — refuses while `parsing`; idempotent.

**New settings** beside the CV block at `config.py:192-216`, matching its style: max upload bytes, max rows, presigned TTL, job timeout. Nothing hardcoded.

- [ ] **Step 1: Write the failing tests**

Cross-tenant 404 on upload, errors and undo. Oversized → 413. A `.xlsx` whose bytes are a PNG → 415. Past the row cap → refused, naming the cap. A stranded import is re-enqueued by the sweep. A failed enqueue leaves `failed`, not `pending`.

- [ ] **Step 2: Implement, then verify and commit**

```bash
git commit -m "Run the import out of the request, and leave a way back"
```

---

### Task 7: Where a migration is watched

**Files:**
- Create: `frontend/app/dashboard/candidates/candidate-imports.tsx`
- Modify: `frontend/app/dashboard/candidates/page.tsx`, `candidates.ts`, `api.ts`, `frontend/app/app.css`

**On the candidates page, not the detail panel** — this is a bulk action on the list.

**Interfaces consumed:** the five routes from Task 6.

- Upload in the avatar's house style — drop or click, no grey button pair, controls revealed on hover **and** `:focus-within` so they never leave the tab order.
- A recent-imports table with state and counts, polled only while `pending` or `parsing`.
- The error report reachable as a download when `rows_failed > 0`.
- **Undo asks for confirmation and says what it will reverse, in counts.** After it runs it reports what it skipped and why — an undo that reversed less than the whole import must say so rather than implying a clean reversal.
- Styles in `app.css` (941 lines), not `globals.css`.
- **Focus must land somewhere sensible after an action removes the control that was pressed.** This codebase has been bitten twice, once because the button being focused was still `disabled` — defer the focus until it is enabled.

- [ ] **Step 1: Types and fetches, then the component, then mount it**

Follow `candidates.ts`'s existing idiom: `credentials: "include"`, `Accept: application/json`, errors through `readError` (`:185`).

- [ ] **Step 2: Verify and commit**

From `frontend/`: `npx tsc --noEmit`, `npm run lint`, `npm run build`. A pre-existing `<img>` LCP warning in `telegram-link-panel.tsx` is expected. Report the `/dashboard/candidates` route size. **You cannot see the rendered page** — it is behind sign-in; do not claim visual verification.

```bash
git commit -m "Let an agency watch its list arrive, and take it back"
```

---

## Self-Review

**Spec coverage.** Both tables and the change trail → Task 1. Bounded inflate shared → Task 2. Sniffing, CSV, XLSX, named columns → Task 3. Row parsing, precision, problems with line numbers → Task 4. Candidate and role matching, conflict-as-problem, import-wins-except-overridden, undo and its restore rule → Task 5. Job, sweep, routes, template, settings → Task 6. UI, undo confirmation, skipped reporting → Task 7. All 14 spec tests appear across Tasks 1-7.

**Placeholders.** None. Four steps deliberately point at a file to copy rather than quoting it — the test fixtures, `CandidateDocument`, `parse_candidate_cv`'s guards, and the upload route — because quoting a signature that may have drifted is worse than naming the source of truth.

**Type consistency.** `sniff_table` returns `"csv" | "xlsx" | None` in Tasks 3 and 6. `read_sheets` is defined in Task 3 and called in Task 6. `CandidateRecord` / `RoleRecord` / `RowProblem` are defined in Task 4 and consumed in Task 5. `apply_import` and `undo_import` are defined in Task 5 and called in Task 6. `bounded_archive` is defined in Task 2 and used in Task 3. `CandidateImport`'s state constants are defined in Task 1 and used in Tasks 5, 6 and 7.
