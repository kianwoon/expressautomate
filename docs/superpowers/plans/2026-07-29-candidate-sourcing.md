# Candidate Sourcing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A recruiter opens a job order, asks for candidates, and gets a ranked shortlist they can explain to a client — with the reasoning shown, and nothing ranked on a protected characteristic.

**Architecture:** A pure scoring module ranks eligible candidates on structured fields alone, so the same inputs always give the same order. An arq job runs it on demand, stores the run, then asks a model to explain only the top few — reading requirements with coded protected-attribute strings removed, and quoting the CV with every quote verified against the stored text.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Alembic, Postgres 16 with RLS, arq on Redis, the existing Cerebras LLM client, pytest, Next.js static export.

## Global Constraints

- All config from the repo-root `.env` via `app.core.config.settings`. **The scoring weights especially — no literals.**
- Every business table carries `tenant_id` via `TenantScoped` (`app/db/base.py:33`), with `ENABLE`/`FORCE ROW LEVEL SECURITY` and a `tenant_isolation` policy in the **same** migration. `verify_rls_enforced()` (`app/db/rls.py:58`) refuses to boot otherwise.
- Every route under `/api`; routers mount on an `api` router that already carries the prefix — do **not** pass `prefix="/api"` again.
- Another agency's opportunity is **404, never 403**.
- Every arq job carries `tenant_id` in its payload.
- **No source file over 1500 lines.** Current: `api/opportunities.py` 480, `models/opportunity.py` 102, `workers/tasks.py` 425, `core/config.py` 689, `models/candidate.py` 524, `detail-panel.tsx` 174, `opportunities.ts` 444, `app.css` 1129.
- **§15 — never assert a fact no source states.** A model quote that does not verify is dropped, not shown.
- **Nothing may rank on a protected characteristic.**

**Running the backend suite** — use this and nothing else:

```bash
cd backend && scripts/test-env.sh -q
```

It sources `backend/.env.test` and hides any root `.env`. **Do not hand-roll environment variables and do not copy CI's** — CI uses a different application-role password than the local database, and forcing it produces hundreds of bogus authentication failures that look like flakiness. Baseline: **1099 passed, 1 skipped**. Also `uv run ruff check .`.

**Alembic head: `c8e2b47d5a91`** (`20260729_1500_import_attempts.py`) — verified, not guessed.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/models/sourcing.py` (create) | `CandidateSubmission`, `SourcingRun`, `SourcingMatch` |
| `backend/alembic/versions/20260729_1600_sourcing.py` (create) | Three tables, RLS |
| `backend/app/services/sourcing/text.py` (create) | Token overlap. Pure. |
| `backend/app/services/sourcing/score.py` (create) | Components → score. Pure. |
| `backend/app/services/sourcing/eligible.py` (create) | Who may appear |
| `backend/app/services/sourcing/redact.py` (create) | Coded strings out of what the model reads. Pure. |
| `backend/app/services/sourcing/explain.py` (create) | The model pass |
| `backend/app/services/sourcing/persist.py` (create) | Writing a run and its matches |
| `backend/app/workers/sourcing_jobs.py` (create) | `run_sourcing`. **Not** `jobs.py` |
| `backend/app/workers/settings.py`, `tasks.py` (modify) | Register; join the sweep |
| `backend/app/api/sourcing.py` (create) | Runs and submissions |
| `backend/app/core/config.py` (modify) | `SOURCING_*` settings |
| `frontend/app/dashboard/job-orders-sourcing.tsx` (create) | The shortlist |
| `frontend/app/dashboard/detail-panel.tsx`, `opportunities.ts`, `api.ts`, `app.css` (modify) | Mount, types, paths, styles |

---

### Task 1: The three tables

**Files:**
- Create: `backend/app/models/sourcing.py`, `backend/alembic/versions/20260729_1600_sourcing.py`
- Test: `backend/tests/test_sourcing_models.py`

**Interfaces produced:**
- `CandidateSubmission` — `candidate_id`, `client_id`, `opportunity_id` (nullable), `submitted_at`, `submitted_by`. **Unique on `(tenant_id, candidate_id, client_id)`** — a person is either in front of that client or not, and a double-click must not make them twice submitted.
- `SourcingRun` — `opportunity_id`, `state`, `candidates_considered`, `shortlisted`, `model_name`, `prompt_version`, `attempts`, `created_by`, `protected_attribute_noticed` (bool), `protected_attribute_note` (text). Constants `PENDING`, `RUNNING`, `DONE`, `FAILED`.
- `SourcingMatch` — `run_id`, `candidate_id`, `score` (Numeric), `reasons` (JSONB), `explanation` (nullable), `explanation_evidence` (nullable).

`protected_attribute_noticed` and `_note` exist because the model is instructed to report a plainly-worded protected requirement, and a report with nowhere to go is a comment rather than a safeguard.

Model these on `CandidateDocument` / `CandidateImport` in `app/models/candidate.py` — same mixins, same composite-FK idiom, same constant style. **A new file** because `models/candidate.py` is 524 lines and these are not candidate-owned.

- [ ] **Step 1: Write the failing tests**

Use the suite's real fixtures — `agency`, `other_agency`, `_a_candidate_row` from `tests/test_candidate_roles_api.py`, `AdminSessionLocal` from `tests.conftest`, `tenant_session` from `app.db.rls`. There is no `client` or `candidate_factory` fixture.

**`pytest.raises` must wrap the whole `async with tenant_session(...)` block**, not sit inside it — the context manager commits again on exit, and in an aborted transaction that raises `PendingRollbackError` outside your `raises`. Three earlier tasks learned this.

Tests: isolation on each of the three tables; a second submission of the same candidate to the same client violates the unique constraint; `state` rejects a value outside its whitelist.

- [ ] **Step 2: Models, then migration**

`down_revision = "c8e2b47d5a91"`. RLS predicate copied from `20260729_1300_candidate_imports.py`, which copied it from `20260726_1800_row_level_security.py:93-102`. Grant DML with `settings.DATABASE_APP_ROLE` — never a literal.

- [ ] **Step 3: Verify and commit**

`scripts/test-env.sh -q`, `uv run ruff check .`, `uv run alembic check` (no new upgrade operations). Never migrate production.

```bash
git commit -m "Give a shortlist somewhere to live, and a submission somewhere to be recorded"
```

---

### Task 2: Comparing two strings, and two salaries

**Files:**
- Create: `backend/app/services/sourcing/__init__.py`, `backend/app/services/sourcing/text.py`
- Test: `backend/tests/test_sourcing_text.py`

**Pure module — no database, no settings, no I/O.**

**Interfaces produced:**
- `tokens(value: str) -> frozenset[str]` — lower-cased, punctuation stripped, split on whitespace.
- `overlap(a: str, b: str) -> float` — 0.0 to 1.0. Define it precisely and document which side is the denominator; a Jaccard and a containment score rank differently, and the choice must be stated rather than emergent.
- `salary_fit(candidate_amount, candidate_currency, candidate_period, job_min, job_max, job_currency, job_period) -> float | None`

**The salary rules, exactly:**
- **Returns `None` when the currencies differ**, or when either side is missing. `None` means "no signal", which the caller reports rather than scoring as zero — a missing signal and a bad fit are different facts.
- **Periods are normalised to a common basis before comparing.** A candidate expecting 6,000 monthly against a job paying 90,000 a year is a good fit; compared naively it is a catastrophic mismatch. `salary_period` is constrained to `hour|day|week|month|year` (`opportunity.py:61`).
- **Nothing is converted between currencies.** A rate we did not fetch on a date we did not record is a fabricated fact.

Nothing in `backend/app/` computes string similarity today — confirmed. `_overlaps` in `cv/persist.py` is date ranges, not text.

- [ ] **Step 1: Write the failing tests**

`overlap("Senior Staff Nurse", "staff nurse")` scores highly; two unrelated titles score near zero; punctuation and case are irrelevant. `salary_fit` returns `None` for SGD against USD; a monthly expectation inside an annual band scores well; an expectation far above the band scores poorly; either side missing returns `None`.

- [ ] **Step 2: Implement, verify, commit**

```bash
git commit -m "Compare a title to a title, and a wage to a wage"
```

---

### Task 3: The score, and who is eligible for one

**Files:**
- Create: `backend/app/services/sourcing/score.py`, `backend/app/services/sourcing/eligible.py`
- Test: `backend/tests/test_sourcing_score.py`, `backend/tests/test_sourcing_eligible.py`

**Interfaces consumed:** `tokens`, `overlap`, `salary_fit` (Task 2); `span_months`, `union_months`, `derive` from `app/services/candidate_tenure.py` (pure); `normalize_skill` (`candidate_naming.py:83`); `normalize_company_name` (`client_naming.py:39`).

**Interfaces produced:**
- `Component` — `name`, `weight`, `raw`, `contribution`, `note`.
- `score_candidate(opportunity, candidate, roles, skills, *, weights, today) -> tuple[Decimal, list[Component]]`
- `eligible_candidates(session, *, tenant_id, client_id) -> list[...]`

**Scoring rules:**
- Every component returns a named, signed contribution. **The weights come from `settings`** — no literals anywhere in this module.
- **A component with no data reports `None` and a note, and is excluded from the total** rather than scoring zero. Absent and bad are different, and a recruiter reading the breakdown must be able to tell them apart.
- Skills: `opportunity.skills` (an `ARRAY(Text)` of raw strings, `opportunity.py:49`) through `normalize_skill`, against `candidate_skills.skill_normalized`.
- Employer signal: `employer_normalized` against `company_name_normalized`.
- Tenure and recency come from the role spans via `candidate_tenure`.
- **No component may read anything that encodes a protected characteristic.** Experience stays — it is job-related and stated — but nothing infers or uses an age.

**Eligibility:** `record_status == active`, `pipeline_stage != placed`, and **not already submitted to this client**. `rejected` candidates **are** included: a rejection was against one role and says nothing about this one.

- [ ] **Step 1: Write the failing tests**

One per rule, plus: the same inputs produce the same score twice; a component with no data is excluded and noted, not zeroed; a submitted candidate disappears and reappears when the submission is deleted.

- [ ] **Step 2: Implement, verify, commit**

```bash
git commit -m "Rank on what the record actually says, and say what it did not"
```

---

### Task 4: Keeping a protected attribute away from the model

**Files:**
- Create: `backend/app/services/sourcing/redact.py`
- Test: `backend/tests/test_sourcing_redact.py`

**Pure module.** Small, and the most important in the plan.

**Interfaces produced:** `redact(text: str, codes: list) -> tuple[str, list[str]]` — the text with each code's verbatim string removed, and the list of what was removed.

**Why it works this way.** `OpportunityCode` (`app/models/opportunity_code.py:30-48`) stores `code` (String 64, verbatim), `meaning`, `attribute`, `start_char` and `end_char`. **Those offsets index the source email, not the extracted `requirements` field** — `detect(source, entries)` (`app/services/ingest/glossary.py:157`) runs over the whole message — so they cannot cut spans out of the text the model reads. The verbatim `code` string can.

**State the limit in the module docstring:** this catches coded discrimination, which is what the glossary exists for. It does **not** catch "female preferred" written out in plain words. That is why the prompt in Task 5 also instructs the model, and why what the model reports is stored.

Only codes whose `attribute` is not null are redacted. A code meaning "night shift" is not a protected characteristic and removing it would damage a legitimate requirement.

- [ ] **Step 1: Write the failing tests**

A code with an `attribute` is removed and reported; a code without one is left alone; a code appearing twice is removed both times; matching is case-insensitive; text with no codes is returned unchanged.

- [ ] **Step 2: Implement, verify, commit**

```bash
git commit -m "Take the coded requirement out before anything reads it"
```

---

### Task 5: Asking the model why, and checking that it is true

**Files:**
- Create: `backend/app/services/sourcing/explain.py`
- Test: `backend/tests/test_sourcing_explain.py`

**Interfaces consumed:** `redact` (Task 4); `_attempts()` from `app/services/ingest/extract.py:72` (pure configuration — fast model at low effort, then strong at high); `verify(field: ExtractedField, source: str) -> bool` from `app/services/ingest/evidence.py:178` — **two arguments, verified**; `complete_json` from `app/services/llm/client.py`.

**Model this on `app/services/cv/extract.py`** — its `PROMPT` shape and two-pass loop. **Do not import `extract_cv`**; it validates a CV schema and formats a CV prompt. What carries over is the shape, not the code.

**Interfaces produced:** `explain_matches(opportunity, redacted_requirements, candidates, *, llm=None) -> tuple[list[Explanation], ProtectedReport]`

**The rules:**
- Only the top N, N from `settings`.
- Every quote is **verified against the candidate's stored text** before it is kept. A quote that does not verify means **no explanation for that candidate** — they keep their deterministic score. An unsupported reason about a person is worse than none.
- The prompt instructs the model to **ignore any requirement about a protected characteristic and to report it**. That report becomes `ProtectedReport` and is stored on the run in Task 6.
- Fast model first, escalating only on low confidence (§32).

- [ ] **Step 1: Write the failing tests**

Drive the model with a stub — read `backend/tests/test_cv_extract.py` first and match how it injects `llm=`. Tests: a verified quote survives; an unverifiable quote yields no explanation but leaves the score; a redacted code never appears in the prompt the stub receives; a reported protected requirement is returned; **no real model is called**.

- [ ] **Step 2: Implement, verify, commit**

```bash
git commit -m "Let the model say why, and only where the page agrees"
```

---

### Task 6: The run

**Files:**
- Create: `backend/app/services/sourcing/persist.py`, `backend/app/workers/sourcing_jobs.py`
- Modify: `backend/app/workers/settings.py`, `backend/app/workers/tasks.py`, `backend/app/core/config.py`
- Test: `backend/tests/test_sourcing_job.py`

**`run_sourcing(ctx, *, tenant_id, opportunity_id, run_id)` goes in a new `sourcing_jobs.py`** — `jobs.py` is at its ceiling. Model it on `run_candidate_import` (`app/workers/import_jobs.py:166`): the same `_RESUMABLE` guard, the same **conditional `UPDATE ... WHERE state IN (...)` claim** that makes the state transition atomic, and the same `attempts` increment inside that claim so a deterministically crashing run cannot loop for ever.

Register in `settings.py`'s `functions` list with `func(run_sourcing, timeout=settings.SOURCING_JOB_TIMEOUT_SECONDS)`, as the other two jobs are (`settings.py:71-108`).

**Join the sweep.** `rescan_stuck` (`app/workers/tasks.py:121-154`) already queries `_STALLED`, `_STALLED_DOCUMENTS` and `_STALLED_IMPORTS`. Add a fourth for runs stranded in `pending` or `running`, counted in its return. A `SECURITY DEFINER` resolver will be needed as `stalled_candidate_imports` was, because the table has FORCE RLS and the sweep sets no tenant — copy that migration's shape, and return **only ids and state**, never content.

**New `SOURCING_*` settings** beside the `IMPORT_*` block at `config.py:218-251`, matching its style: the component weights, top-N for the model pass, job timeout, max attempts, and a per-tenant daily cap. **Nothing hardcoded.**

**The stored run must not change when candidate data later does** — matches are written with their score and reasons, and read back as written.

- [ ] **Step 1: Write the failing tests**

A run stores its matches with scores and reasons; re-reading after a candidate's roles change returns the original; a stranded run is re-enqueued; a repeatedly failing run reaches `failed` and stops; the protected report lands on the run.

- [ ] **Step 2: Implement, verify, commit**

```bash
git commit -m "Run the search out of the request, and keep what it found"
```

---

### Task 7: The routes and the shortlist

**Files:**
- Create: `backend/app/api/sourcing.py`, `frontend/app/dashboard/job-orders-sourcing.tsx`
- Modify: `backend/app/main.py`, `frontend/app/dashboard/detail-panel.tsx`, `opportunities.ts`, `api.ts`, `app.css`
- Test: `backend/tests/test_sourcing_api.py`

**Routes:**
- `POST /api/opportunities/{id}/sourcing` → **202**. On a `False` from `enqueue()` — it returns a bool and never raises — the run goes to `failed` with a retryable message. Follow `candidate_imports.py:212-291`.
- `GET /api/opportunities/{id}/sourcing` → the latest run and its matches, **ordered by score then `candidate_id`** so an equal-scoring pair reads the same way every time.
- `GET /api/opportunities/{id}/sourcing/{run_id}` → an earlier run.
- `POST /api/candidates/{id}/submissions` and `DELETE /api/candidates/{id}/submissions/{submission_id}`. Without these the eligibility exclusion never fires.

**Register `sourcing.router` carefully.** `/opportunities/{id}/sourcing` must not be swallowed by an existing `/opportunities/{opportunity_id}` route — the import feature hit exactly this, and a comment at the include site plus a test is how it was settled.

**UI:** in the job order detail panel (`detail-panel.tsx`, 174 lines) — a "Find candidates" action, then the ranked list showing each score's breakdown and the model's reason where there is one. A "Mark submitted" action per row. Where the job order references a protected attribute **or the model reported one**, a notice says plainly that the shortlist ignored that requirement.

Poll only while a run is `pending` or `running`. Styles in `app.css` (1129 lines), not `globals.css`. Focus must land somewhere sensible after an action removes the control that was pressed — **defer it until the control is enabled**; this codebase has been bitten by that twice.

- [ ] **Step 1: Write the failing API tests**

Cross-tenant 404 on start, read and submissions. A duplicate submission is refused. Deleting a submission restores eligibility. No route escapes `/api`.

- [ ] **Step 2: Implement backend, then frontend, verify, commit**

Backend: `scripts/test-env.sh -q`, `uv run ruff check .`. Frontend: `npx tsc --noEmit`, `npm run lint`, `npm run build` — a pre-existing `<img>` LCP warning in `telegram-link-panel.tsx` is expected. Report the `/dashboard` route size. **You cannot see the rendered page** — it is behind sign-in; do not claim visual verification.

```bash
git commit -m "Show who fits, and why, and let a recruiter say they sent them"
```

---

## Self-Review

**Spec coverage.** Three tables and the unique constraint → Task 1. Token overlap, salary with period and currency → Task 2. Components, weights from settings, eligibility → Task 3. Redaction and its stated limit → Task 4. Model pass, quote verification, the protected report → Task 5. Storage, job, sweep, attempts → Task 6. Routes, submissions, UI, ordering → Task 7. All 16 spec tests appear across Tasks 1-7.

**Placeholders.** None. Four steps deliberately point at a file to copy rather than quoting it — the test fixtures, `CandidateDocument`, `run_candidate_import`'s claim, and the LLM stub — because quoting a signature that may have drifted is worse than naming the source of truth.

**Verified rather than assumed.** The alembic head is `c8e2b47d5a91`, not the revision an earlier scout reported; `verify` takes two arguments, not three. Both were checked against the files.

**Type consistency.** `tokens`/`overlap`/`salary_fit` are defined in Task 2 and consumed in Task 3. `Component` is defined in Task 3 and serialised in Tasks 6 and 7. `redact` is defined in Task 4 and called in Task 5. `explain_matches` is defined in Task 5 and called in Task 6. `SourcingRun`'s state constants are defined in Task 1 and used in Tasks 6 and 7.
