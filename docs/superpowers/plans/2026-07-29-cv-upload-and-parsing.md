# CV Upload and Parsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A recruiter uploads a CV; a model reads it; the roles and skills it finds arrive as proposals a person confirms, each carrying a quote from the document that can be checked against the stored text.

**Architecture:** The upload is stored in R2 and recorded in a new `candidate_documents` table. An arq job extracts text locally with `pypdf` / `python-docx`, runs a CV-specific two-pass extraction modelled on the email pipeline's discipline, verifies every quoted span against the stored text, and writes roles and skills that do not already exist. The existing `Extraction` provenance tables are generalised first, because today they can only describe an email.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Alembic, Postgres 16 with RLS, arq on Redis, Cerebras via the existing LLM client, pypdf, python-docx, pytest, Next.js static export.

## Global Constraints

- All config from the repo-root `.env` via `app.core.config.settings`. **No hardcoded URLs, model names, keys, limits, TTLs or quotas.**
- Every business table carries `tenant_id` via `TenantScoped` (`app/db/base.py:33`).
- **No source file may exceed 1500 lines.** Current: `jobs.py` **1443**, `candidates.py` 851, `config.py` 628, `app.css` 797, `candidate-history.tsx` 693. The CV job **cannot** go in `jobs.py`.
- Every route under `/api`; `tests/test_routing.py` fails if one escapes.
- Another agency's candidate is **404, never 403**.
- **§15 — never assert a fact no source states.** No date component may be invented; a span that does not verify does not become a role.
- Every arq job carries `tenant_id` in its payload.
- Tests never touch the live database; `conftest.py` refuses a non-local host.

**Running the backend suite** (the worktree `.env` points at production, which conftest correctly refuses):

```bash
cd backend && DATABASE_ADMIN_URL=postgresql://postgres:postgres@localhost:5433/expressautomate DATABASE_URL=postgresql://expressautomate_app:ci-app-password@localhost:5433/expressautomate uv run pytest -q
```

Baseline before this plan: **847 passed**. Alembic head: `b7c1e4a2d905` (`20260728_1800_candidate_roles.py`).

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/models/extraction.py` (modify) | Let an extraction describe a CV, not only an email |
| `backend/app/models/candidate.py` (modify) | `CandidateDocument`; `source`/`status` on `CandidateSkill` |
| `backend/alembic/versions/20260729_1000_extraction_sources.py` (create) | Generalise provenance |
| `backend/alembic/versions/20260729_1100_candidate_documents.py` (create) | New table + RLS; skill columns |
| `backend/app/services/cv/text.py` (create) | Bytes → text. Sniffing, PDF, DOCX. Pure. |
| `backend/app/services/cv/schema.py` (create) | The CV response model and its JSON schema |
| `backend/app/services/cv/extract.py` (create) | Prompt, two-pass, span verification |
| `backend/app/services/cv/persist.py` (create) | Extraction rows, matching, role and skill inserts |
| `backend/app/workers/cv_jobs.py` (create) | `parse_candidate_cv`. **Not** `jobs.py` — it is at 1443 lines |
| `backend/app/workers/settings.py` (modify) | Register the job |
| `backend/app/workers/tasks.py` (modify) | Add documents to the stuck sweep |
| `backend/app/api/candidate_documents.py` (create) | Upload, download, delete, quota |
| `backend/app/api/candidate_roles.py` (modify) | Confirm and reject endpoints |
| `backend/app/core/config.py` (modify) | CV settings |
| `frontend/app/dashboard/candidates/candidate-cv.tsx` (create) | Upload control and parse state (Task 7) |
| `frontend/app/dashboard/candidates/candidate-history.tsx` (modify) | Enable confirm/reject; show evidence |

---

### Task 1: Let provenance describe something other than an email

**Files:**
- Modify: `backend/app/models/extraction.py:25-63`
- Create: `backend/alembic/versions/20260729_1000_extraction_sources.py`
- Test: `backend/tests/test_extraction_sources.py`

**This task exists because the feature is impossible without it.** `Extraction.email_message_id` is `nullable=False` with an FK to `email_messages` (`extraction.py:28-33`). A CV has no email, so no extraction row can be written for one. `ExtractionEvidence.opportunity_id` has the same shape: it can point at a vacancy, not a role.

**Interfaces produced:** `Extraction.candidate_document_id`, `ExtractionEvidence.candidate_role_id`, and a CHECK constraint on each ensuring exactly one source.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_extraction_sources.py`. Use the fixture idiom from `tests/test_candidate_roles_api.py` (`agency`, `_a_candidate_row`, `AdminSessionLocal`, `tenant_session`) — do not invent fixtures.

```python
"""An extraction can describe an email or a CV, and must say which."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.extraction import Extraction


@pytest.mark.asyncio
async def test_an_extraction_with_neither_source_is_refused(agency):
    """Provenance that names no source is not provenance."""
    tenant_id, _user = agency
    async with tenant_session(tenant_id) as session:
        session.add(
            Extraction(
                tenant_id=tenant_id,
                email_message_id=None,
                candidate_document_id=None,
                model_name="x",
                prompt_version="v1",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
```

Add a second test asserting an extraction naming **both** sources is refused, and a third asserting one naming only `candidate_document_id` is accepted.

- [ ] **Step 2: Run it, watch it fail**

Expected: `TypeError` or `AttributeError` on `candidate_document_id` — the column does not exist.

- [ ] **Step 3: Change the model**

In `backend/app/models/extraction.py`, make `email_message_id` nullable and add the sibling. Keep the file's comment voice — it explains *why*, in prose.

```python
    # Nullable since a CV extraction has no email behind it. The CHECK below is
    # what keeps this honest: provenance that names no source, or two, is not
    # provenance at all.
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("email_messages.id", ondelete="CASCADE"),
        index=True,
    )
    candidate_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidate_documents.id", ondelete="CASCADE"),
        index=True,
    )
```

with

```python
    __table_args__ = (
        CheckConstraint(
            "(email_message_id IS NULL) <> (candidate_document_id IS NULL)",
            name="ck_extractions_exactly_one_source",
        ),
    )
```

Do the same on `ExtractionEvidence`: add `candidate_role_id` beside `opportunity_id`. **Both stay nullable with no CHECK** — evidence may legitimately reference neither while it describes a field of the source document itself. Say that in a comment, so a later reader does not "fix" it into a constraint.

**Ordering note:** this migration references `candidate_documents`, which Task 2 creates. So the FK on `candidate_document_id` is added in **Task 2's** migration, not this one. This migration only makes `email_message_id` nullable, adds the bare column, and adds `candidate_role_id`. Task 2 adds both foreign keys and the CHECK. Write it that way; do not reorder the tasks.

- [ ] **Step 4: Migration, tests, commit**

Revision on top of `b7c1e4a2d905`. Run the suite and `uv run alembic check` — it must report no new upgrade operations. Never run migrations against production.

```bash
git commit -m "Let an extraction say what it read, when that was not an email"
```

---

### Task 2: The document table, and skills that can be proposed

**Files:**
- Modify: `backend/app/models/candidate.py` (append `CandidateDocument`; add two columns to `CandidateSkill` at `:139-163`)
- Create: `backend/alembic/versions/20260729_1100_candidate_documents.py`
- Test: `backend/tests/test_candidate_documents.py`

**Interfaces produced:** `CandidateDocument` with `filename`, `content_type`, `byte_size`, `object_key`, `text_key`, `text_chars`, `parse_state`, `parse_error`, `uploaded_by`; class constants `PENDING`, `PARSING`, `PARSED`, `UNREADABLE`, `FAILED`, `PARSE_STATES`. `CandidateSkill.source` and `.status` mirroring `CandidateRole`'s values.

- [ ] **Step 1: Write the failing tests**

Isolation first, exactly as `tests/test_candidate_roles_api.py` does it: a document written in agency A's session is invisible in agency B's. Then a test that `parse_state` rejects a value outside the whitelist.

- [ ] **Step 2: Model**

`CandidateDocument`, `TenantScoped`, composite `(tenant_id, candidate_id)` FK with `ondelete="CASCADE"`, following `CandidateRole` at `candidate.py:180-260`. Carry the reasoning in the docstring: the extracted text is stored because an evidence span is an offset into it; `unreadable` and `failed` are separate because one answers the same way forever and the other is a bad minute.

On `CandidateSkill`, add:

```python
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="human")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
```

Existing rows are human-typed, so the server defaults make the backfill correct with no data migration.

- [ ] **Step 3: Migration**

Creates `candidate_documents` with `ENABLE`/`FORCE ROW LEVEL SECURITY` and the `tenant_isolation` policy in the same revision — copy the predicate from `20260728_1800_candidate_roles.py`, which copied it from `20260726_1800_row_level_security.py:93-102`. Grant DML to `settings.DATABASE_APP_ROLE`; **no literal role name.**

Then add the two `candidate_skills` columns with server defaults, and **the two foreign keys plus the CHECK deferred from Task 1**.

- [ ] **Step 4: Verify and commit**

Suite green, `alembic check` clean, `ruff check .` clean.

```bash
git commit -m "Give a candidate the documents they came with"
```

---

### Task 3: Bytes to text

**Files:**
- Create: `backend/app/services/cv/text.py`, `backend/app/services/cv/__init__.py`
- Modify: `backend/pyproject.toml` (add `pypdf`, `python-docx`), then `uv sync`
- Test: `backend/tests/test_cv_text.py`

**Interfaces produced:**
- `sniff(data: bytes) -> str | None` — returns `"pdf"`, `"docx"`, or `None`.
- `extract_text(data: bytes, kind: str) -> str`
- `UnsupportedDocument(Exception)`

- [ ] **Step 1: Write the failing tests**

```python
"""Turning an uploaded file into text, and refusing what we cannot read."""

from app.services.cv.text import sniff


def test_a_pdf_is_recognised_by_its_bytes():
    assert sniff(b"%PDF-1.7\nrest of file") == "pdf"


def test_a_bare_zip_is_not_a_docx():
    """A DOCX is a zip, so `PK` alone proves nothing.

    Accepting any archive means a recruiter can hand us a zip of holiday
    photos and get an unreadable job instead of a straight refusal.
    """
    assert sniff(b"PK\x03\x04" + b"\x00" * 64) is None


def test_a_real_docx_is_recognised(tmp_path):
    """Built here rather than committed as a fixture binary: the point is the
    presence of `word/document.xml` inside the archive, and that is clearer
    written than checked in."""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
    assert sniff(buf.getvalue()) == "docx"


def test_something_that_is_neither_is_refused():
    assert sniff(b"just some text") is None
```

Add: a PDF with no extractable text returns `""` (the caller turns that into `unreadable`, not this module), and `extract_text` on a corrupt PDF raises `UnsupportedDocument` rather than propagating a library error.

- [ ] **Step 2: Run, watch fail, implement, run again**

`sniff` checks `%PDF-` for PDFs; for DOCX it opens the bytes as a zip and requires `word/document.xml` to be present. Never trust the filename or the client's `Content-Type`.

- [ ] **Step 3: Commit**

```bash
git commit -m "Read a CV by what it is, not by what it is called"
```

---

### Task 4: The CV extraction pipeline

**Files:**
- Create: `backend/app/services/cv/schema.py`, `backend/app/services/cv/extract.py`
- Test: `backend/tests/test_cv_extract.py`

**Interfaces consumed:** `_attempts()` from `app/services/ingest/extract.py:72` — **pure configuration, reusable as-is**. `complete_json(prompt, *, model, schema, ...) -> LLMResult` from `app/services/llm/client.py:49`, raising `LLMInvalidJSON`. `verify(field, source) -> bool` from `app/services/ingest/evidence.py:178`. `NOT_MENTIONED` from `app/services/ingest/schema.py:18`.

**Interfaces produced:** `CVResponse` (roles and skills, each field carrying `value` / `evidence` / `start_char` / `end_char` / `confidence`, mirroring `ExtractedField` at `ingest/schema.py:39-69`); `cv_json_schema() -> dict`; `extract_cv(text: str, *, llm=None) -> tuple[CVResponse, LLMResult]`.

**Do not import `ingest.extract.extract()`.** It validates against `ExtractionResponse` — the *vacancy* schema (`ingest/extract.py:26,129`) — and formats `PROMPT.format(email=…)`. Only the shape carries over: two passes, spans verified against the source, escalate only on proof.

- [ ] **Step 1: Write the failing tests**

Drive the LLM with a stub, as `tests/test_extract_job.py` does — read it first and match how it injects `llm=`.

Tests that must exist:
- A role whose `evidence` span genuinely appears in the text verifies, and survives.
- A role whose quoted evidence is **not** in the text triggers escalation to the strong model.
- Evidence that still fails after escalation **does not become a role**.
- A field the CV does not state comes back `NOT_MENTIONED` and is not invented.
- A date given as "Mar 2019" yields month precision; no day appears anywhere in the result.

- [ ] **Step 2: Implement**

`PROMPT` is a module constant formatted with `not_mentioned`, `schema` and `cv`, mirroring `ingest/extract.py:32-53`. The prompt must instruct: quote verbatim, give character offsets, report the precision actually seen, and answer `Not mentioned` rather than guess.

The two-pass loop mirrors `ingest/extract.py:116-146` — iterate `_attempts()`, return on the first result whose spans verify, keep the last otherwise.

- [ ] **Step 3: Verify and commit**

```bash
git commit -m "Read a career off a CV, and prove each line came from the page"
```

---

### Task 5: Storing what was read, and the job that reads it

**Files:**
- Create: `backend/app/services/cv/persist.py`, `backend/app/workers/cv_jobs.py`
- Modify: `backend/app/workers/settings.py:67-79` (register), `backend/app/workers/tasks.py:103-142` (stuck sweep)
- Test: `backend/tests/test_cv_persist.py`, `backend/tests/test_cv_job.py`

**`parse_candidate_cv` goes in a new `cv_jobs.py`, not `jobs.py`** — that file is 1443 lines against a 1500 ceiling.

**Interfaces produced:**
- `persist_cv(session, *, tenant_id, candidate_id, document, response, result, text) -> None`
- `parse_candidate_cv(ctx, *, tenant_id: str, candidate_id: str, document_id: str) -> None`

- [ ] **Step 1: Write the failing tests**

**The matching rule, stated exactly**, because it is the one thing here an implementer would otherwise guess at:

- Both spans are pinned to month starts before comparison, the same way `candidate_tenure.span_months` does it — a CV giving "Mar 2019" and a typed role giving `2019-03-14` describe the same month and must match.
- Overlap is **half-open**: they overlap iff `start_a < end_b AND start_b < end_a`. Adjacent roles — one ending March 2020, the next starting March 2020 — do **not** overlap, and are two roles.
- An **open-ended** role (`ended_on IS NULL`) is treated as ending at today for the comparison, on both sides.
- A parsed role with **no dates at all** matches on `employer_normalized` alone, and **only if exactly one** existing role has that employer. Two or more and it inserts unconfirmed instead: with nothing to disambiguate on, picking one would be a guess presented as a fact.

Tests for each of those four, plus:
- A parsed role at the same employer with **non-overlapping** dates inserts as a new role — somebody who left and returned held two roles.
- A genuinely new role inserts `source="cv_upload"`, `status="unconfirmed"`, with `extraction_id` set.
- Replaying the job on a `parsed` document inserts nothing.
- A skill already present does not duplicate — `skill_normalized` is unique per candidate (`candidate.py:157-162`).
- A document stranded in `pending` is re-enqueued by the stuck sweep.

- [ ] **Step 2: Implement persistence**

Copy the write pattern from `app/services/ingest/persist.py` rather than inventing one — but **only part of it carries over**. That module is raw SQL and vacancy-shaped; what you want is the `Extraction` insert and the `ExtractionEvidence` loop that sets `evidence_valid` (around `:136-139` and `:446-481`). The opportunity-specific writing around them does not apply. Point the new rows at `candidate_document_id` and `candidate_role_id`.

- [ ] **Step 3: Implement the job**

Signature and guards follow `extract_email` (`jobs.py:701`). Sequence: load document → `parsing` → fetch bytes → `sniff` → `extract_text` → empty text becomes `unreadable` with a sentence naming the cause, and **no retry** → store text to R2, record `text_chars` → `extract_cv` → `persist_cv` → `parsed`.

Register in `settings.py`'s `functions` list.

- [ ] **Step 4: Join the stuck sweep**

`rescan_stuck` (`app/workers/tasks.py:103`) re-enqueues rows stranded by an outage — but it is **welded to email rows**: its `_STALLED` query, its `RESUME_JOB` map and its enqueue payload all assume an email message and a mailbox. "Documents join the sweep" is not an instruction anyone can follow.

Concretely: add a **second query and enqueue block inside the same function**, selecting `candidate_documents` in `pending` or `parsing` older than the existing staleness threshold, enqueuing `parse_candidate_cv` with `tenant_id`, `candidate_id` and `document_id`. It already returns a count of rows requeued; add the documents to that count. One function, two sweeps, because they answer the same question and a second scheduled task is a second thing to forget.

Without this, a worker that dies mid-job strands a CV forever — the case a `False` from `enqueue()` does not cover.

- [ ] **Step 5: Verify and commit**

```bash
git commit -m "Turn a read CV into rows a person can accept or refuse"
```

---

### Task 6: Upload, quota, and the human in the loop — backend

**Files:**
- Create: `backend/app/api/candidate_documents.py`
- Modify: `backend/app/api/candidate_roles.py` (confirm/reject), `backend/app/main.py`, `backend/app/core/config.py`
- Test: `backend/tests/test_candidate_documents_api.py`

**Interfaces produced, which Task 7 codes against:**
- `POST /api/candidates/{id}/documents` → **202** `{id, filename, parse_state, ...}`; **413** oversized; **415** unsupported; **429** past quota with nothing stored.
- `GET /api/candidates/{id}/documents/{doc_id}/download` → `{url, expires_in}`.
- `DELETE /api/candidates/{id}/documents/{doc_id}` → 204.
- `POST /api/candidates/{id}/roles/{role_id}/confirm` and `/reject` → the updated role.
- Documents embedded in the candidate GET as `documents`.

**New settings** in `config.py`, beside the avatar block at `:167-179`, matching its style and validation: max upload bytes, max extracted characters, presigned download TTL, and **a per-tenant daily parse quota**. Nothing hardcoded.

**Where the daily count lives: nowhere new.** It is a `COUNT(*)` of that tenant's `candidate_documents` rows created since midnight UTC, run inside the tenant-scoped session so RLS does the scoping for free. No counter table, no Redis key, nothing to drift out of step with reality.

Two consequences, both accepted deliberately: deleting a document frees quota, and a document that failed to enqueue still consumed one. The first is a hole only somebody deliberately gaming their own bill would use; the second is the safer direction to err. A counter table would fix both and add a row that can disagree with the documents it counts, which is a worse problem than either.

- [ ] **Step 1: Write the failing API tests**

Cross-tenant is 404 on upload, download and delete. Oversized is 413. A `.pdf` whose bytes are a PNG is 415. Past the daily quota is **429 with nothing stored**. Deleting a document leaves the roles it produced.

- [ ] **Step 2: Implement the routes**

Follow `candidates_avatar.py` for **ordering and the size cap** — `_read_within_limit` at `:73-91` caps before the body is fully read. Do **not** follow it for type checking: it is built around `Image.open()`, which no PDF satisfies. Use `sniff()` from Task 3.

Upload stores to R2, inserts `pending`, enqueues, returns **202**. On a `False` from `enqueue()` the row goes to `failed` with a retryable message — `enqueue` returns a bool and never raises (`queue.py:84`), so a silent Redis outage would otherwise leave the document pending forever.

- [ ] **Step 3: Confirm and reject**

`POST /api/candidates/{id}/roles/{role_id}/confirm` and `/reject`. Piece 1 shipped these buttons disabled with nothing behind them (`candidate-history.tsx:502-513`); this is where they come alive. Confirming re-runs the derivation from Task 3 of the previous plan, because a confirmed role changes the candidate's current employer.

- [ ] **Step 4: Verify and commit**

Backend suite and `ruff check .`.

```bash
git commit -m "Take a CV from a recruiter, and let them answer for what it found"
```

---

### Task 7: Upload, parse state, and the evidence — frontend

**Files:**
- Create: `frontend/app/dashboard/candidates/candidate-cv.tsx`
- Modify: `frontend/app/dashboard/candidates/candidate-history.tsx` (enable confirm/reject, show evidence), `candidates.ts`, `api.ts`, `frontend/app/app.css`

**Interfaces consumed:** the six routes and the `documents` array from Task 6, exactly as listed there.

- [ ] **Step 1: Types and fetch functions**

`CandidateDocument` in `candidates.ts`, plus `documents?: CandidateDocument[]` on `Candidate` — optional, because the list endpoint does not send it and absent must mean "not loaded", never "none". Follow the file's existing fetch idiom: `credentials: "include"`, `Accept: application/json`, errors through `readError` (`candidates.ts:185`).

- [ ] **Step 2: The upload control**

`candidate-cv.tsx` in the avatar's house style — drop a file on it or click it, no grey button pair, controls revealed on hover **and** `:focus-within` so they never leave the tab order. Parse state renders inline.

**The `unreadable` message must name the cause and the way forward:** the file looks like a scan rather than a text document, the roles can be typed meanwhile, and reading scans is coming. To a recruiter holding a scanned CV, a bare refusal is indistinguishable from a broken product.

- [ ] **Step 3: Turn on confirm and reject**

`candidate-history.tsx:502-513` renders these buttons disabled with a comment saying they wait for the CV parser. Wire them to the Task 6 endpoints and drop the comment.

**An unconfirmed role shows the quoted line from the CV that produced it.** That is the payoff of everything above: not "the AI says she worked here", but "the CV says this, here."

Styles go in `app.css` (797 lines), not `globals.css`. Respect `prefers-reduced-motion`.

- [ ] **Step 4: Verify and commit**

From `frontend/`: `npx tsc --noEmit`, `npm run lint`, `npm run build`. Report the `/dashboard/candidates` route size. You cannot see the rendered page — it is behind sign-in; do not claim visual verification.

```bash
git commit -m "Show what the CV said, and let a recruiter agree or not"
```

---

## Self-Review

**Spec coverage.** Local text extraction → Task 3. Retention of file and text → Tasks 2, 5. Roles and skills → Tasks 4, 5. Matching → Task 5. `unreadable` vs `failed` → Tasks 2, 5. Two-pass escalation and span verification → Task 4. Stuck sweep → Task 5. Quota → Task 6. Confirm/reject → Task 6. Upload UI and evidence display → Task 7. All 13 spec tests appear across Tasks 2-7.

**Beyond the spec, and necessary.** Task 1 exists because `Extraction.email_message_id` is NOT NULL against `email_messages` (`extraction.py:28-33`) — the spec assumed an `extraction_id` that cannot currently be created. Task 5 puts the job in a new module because `jobs.py` is at 1443 of 1500 lines. Neither was in the spec; both block delivery.

**Placeholders.** None. Three steps deliberately point at a file to copy rather than quoting it — the test fixtures, `ingest/persist.py`'s write pattern, and the LLM stub in `test_extract_job.py` — because quoting a signature that may have drifted is worse than naming the source of truth.

**Type consistency.** `sniff` returns `"pdf" | "docx" | None` in Tasks 3, 5 and 6. `extract_cv(text, *, llm=None)` is defined in Task 4 and called in Task 5. `persist_cv` is defined and called in Task 5 only. `CandidateDocument`'s state constants are defined in Task 2 and used in Tasks 5 and 6.
