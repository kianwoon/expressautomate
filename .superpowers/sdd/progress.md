# Candidate work history — SDD progress

Plan: docs/superpowers/plans/2026-07-28-candidate-work-history.md
Branch: kianwoon/agent-data-generation-clients-97f181

Task 1: complete (commits a00a92a..5027667, review clean)
Task 2: complete (commits 5027667..63a6f4c, review clean)
Task 3: complete (commits 63a6f4c..07b33d0, review clean after 2 fix rounds)
  Owner reversal: deleting last role now CLEARS derived columns (spec amended in 07b33d0)
  MINOR for final review: candidate_roles.py:193 - roles remain but all undated =>
    derive() returns years_experience=None and the stale cached value survives.
    Same section-15 argument as the deletion reversal. Fix: assign unconditionally when not overridden.
  UNPINNED for final review: no test for deleting only the current role while older roles remain.
Task 4: complete (commits 07b33d0..bdfc939, review clean, 272/272 selectors verified)
Task 5: complete (commits bdfc939..28ec8f6, focus race fixed)
Final review: READY WITH FIXES -> fixed in fefca5b. 847 passed. Feature complete, NOT merged.

## Piece 2 — CV upload and parsing
Plan: docs/superpowers/plans/2026-07-29-cv-upload-and-parsing.md
P2 Task 1: complete (36e4206..0c5b66f, review clean; 4 items carried to Task 2)
P2 Task 2: complete (0c5b66f..11dbd08, review clean, 853 passed 0 skipped)
P2 Task 3: complete (11dbd08..b0c08bb, bounded DOCX inflate + real PDF early-stop test, 862 passed)
P2 Task 3: FINAL (11dbd08..754c944, 863 passed). Fable 8/10 SOUND TO BUILD ON.
  CARRY TO TASK 5: PDF bound is only between pages - a single-page FlateDecode bomb
    inflates inside pypdf unwatched. Mitigate with upload byte cap (Task 6) + arq job timeout.
P2 Task 4: complete (31d7802..bccdd91). Fable 8.5/10 APPROVE WITH CHANGES; precision
  self-licensing hole fixed (evidence alone now licenses precision). Implementer reported
  876 passed BEFORE the local test DB was destroyed.
  CARRY TO TASK 5 (from Fable): record dropped roles/skills with count+reason rather than
    silently removing; make an empty extraction a visible state; persist precision, never widen it.

ENVIRONMENT FIXED — backend/scripts/test-env.sh. 875 passed, 1 skipped, repeatable.
  Root cause was TWO things, neither of which was what I first blamed:
  (1) backend/.env.test is the local convention (DATABASE_APP_PASSWORD=test-app-password,
      port 5433). I forced CI's ci-app-password over it, so every connection failed auth.
  (2) An interrupted run leaves stranded rows; rescan_stuck then counts 140 where a test
      expects 0. Looks like an auth storm, is actually data pollution. Rebuild the container.
  The script sources .env.test and hides any root .env for the run.
P2 Task 5: complete (2e8e075..2d0847f, 901 passed). Fable 8.5/10 APPROVE WITH CHANGES; both
  Important defects fixed: same-month role could never match (zero-length half-open span),
  and ambiguous numeric dates (both fields <=12) were guessed day-first - now year-only.
  ACCEPTANCE CRITERION FOR TASK 6: the upload route must enqueue parse_candidate_cv.
    Today only rescan_stuck does, so an uploaded CV would sit until the sweep found it.
  DEFERRED: get_bytes and the arq timeout are covered by doubles only - needs a staging smoke.
P2 Task 6: complete (a6a6920..62dc217, 918 passed). Fable 9/10 APPROVE, no fix-now findings.
  Deferred by owner ruling: quota window is UTC (8am SGT) - accepted, revisit if a tenant
    ever hits the cap; R2 exercised via doubles only; multipart is spooled to disk before
    the 413 (Starlette behaviour, matches the avatar precedent).
P2 Task 7: complete (273509e..d22cbc0, 922 passed). Evidence exposure was a PLAN GAP -
  the UI task said "show the evidence" but no task exposed it on the role serializer.
  Closed in d22cbc0 with a single IN-query, evidence_valid only, no offsets or confidence.

PIECE 2 COMPLETE. Fable final: SHIP WITH FIXES 8.5/10; all findings closed in a57e4f0.
  924 passed, ruff/tsc/build clean. NOT pushed, migrations NOT applied to Koyeb.
  BEFORE REAL RECRUITERS (owner ruling): one staging upload to exercise real R2 and the
    arq timeout - both are covered by doubles only today.
  ACCEPTED AS-IS: UTC quota reset, multipart disk-spool before 413, day-precision residual.
  HUMAN MUST CHECK ONCE SIGNED IN: (1) a real PDF reaches parsed with a visible evidence
    quote; (2) a scanned CV shows the unreadable guidance; (3) confirm/reject move the
    candidate's headline fields.
  Final fix reviewed (Fable 7.5/10) - widening the match query for rejected roles had
    introduced two untested regressions: a rejected row made a dateless live match look
    ambiguous, and live-vs-rejected overlap resolved by database row order. Closed in
    cb27aef: live roles are consulted first, rejected only for suppression. 927 passed.

## Piece 4 - candidate/history spreadsheet import
Plan: docs/superpowers/plans/2026-07-29-candidate-spreadsheet-import.md (8 tasks)
P4 Task 1: complete (0a45c06..e874d47, 993 passed, review clean)
  CARRY TO TASKS 5 AND 6: import_id uses a PLAIN FK (composite + bare SET NULL would null
    tenant_id, which is NOT NULL - same trap as merged_into_candidate_id). So nothing at the
    DB level stops import_id pointing at ANOTHER tenant's import. App code must assert
    import.tenant_id == candidate.tenant_id before assigning, and undo must too.
P4 Task 2: complete (e874d47..c08ec4f, 1000 passed, faithful move verified line-by-line)
P4 Task 3: complete (c08ec4f..bcc2d28, 1013 passed; streaming cap + duplicate-header refusal + sheet_name)
P4 Task 4: complete (bcc2d28..479b5af, 1032 passed). Critical fixed: slash-dates were
  reordered opportunistically so 4/25/2019 asserted 25 April, while the CV path drops it.
  Both paths now agree. Header names LOCKED: full name, email, phone, title, employer,
  location, start date, end date, description - Task 7's template must emit exactly these.
  CARRY TO TASK 5: precision None means keep the role, drop only the date (as cv/persist does).
P4 Task 5: complete (479b5af..55171f9, 1050 passed). Fable 8/10 APPROVE WITH CHANGES, closed.
  Line numbers now threaded through the records - index+2 drifted after any dropped row and
  named the wrong Excel row confidently. Rejected roles stay rejected (upheld, now tested).
  CARRY TO TASK 6: undo keys on action=CREATED; field_name="*" on a created change row is
    informational only. Also assert import.tenant_id matches before touching anything.
P4 Task 6: complete (55171f9..5d14429, 1064 passed). Fable 8/10; data-loss path closed -
  deleting a created candidate cascaded away roles a recruiter added AFTER the import.
  Now skipped and reported. DEFERRED by owner: order_by(created_at,id) tiebreak needs a
  monotonic sequence column that does not exist; Decimal/float coercion trap on columns
  not yet importable (fails inert, the safe direction).
  CARRY TO TASK 8 UI: confirm when rows_deleted > 0; a second undo's skips are its OWN
  first pass's work and must NOT be presented as "we protected your edits".
P4 Task 7: complete (5d14429..91fb93e, 1092 passed). Fable 9/10 APPROVE, no fix-now.
  DEFERRED by owner: router include order (comment+test judged sufficient); migration must
    deploy before worker (same accepted contract as the two existing sweep functions);
    a deterministically crashing file loops forever - needs a retry counter, own small task.
  Row cap is async by design: XLSX row count needs inflating the zip, which is the DoS the
    job isolates. Byte cap is the synchronous proxy.
  CARRY TO TASK 8: list_imports has no default limit - bound it in the UI call or add one.
P4 Task 8: complete (91fb93e..bdd00fe, UI). 1092 passed, tsc/build clean, route 15.6 kB.

PIECE 4 COMPLETE. Fable final: SHIP WITH FIXES 8/10; all closed in 8851cd8.
  1097 passed, ruff/tsc/build clean. NOT pushed, migrations NOT applied to Koyeb.
  ACCEPTED AS-IS: order_by tiebreak, Decimal coercion, router include order,
    migration-before-worker deploy contract, list limit.
  KNOWN GAP: a created candidate whose ONLY later work is an avatar upload is still
    deleted by undo - avatar_key is a plain column, not an attached record.
  HUMAN MUST CHECK ONCE SIGNED IN: (1) template -> fill -> upload XLSX -> correct counts
    and candidates visible; (2) undo a small import - confirm sentence, skips list, refresh;
    (3) error-report link downloads via presigned URL.
  BEFORE DEPLOY: check Koyeb api AND worker env, and apply the migration before the worker.
P4 final fix reviewed by Fable (8.5/10 APPROVE WITH CHANGES) and closed in 62ec2d0:
  the conditional-UPDATE claim that makes undo safe was verified only by inspection - both
  sides now have a test that forces the race and fails if the WHERE clause is removed.
  Lost race now answers 409, not 500.
  ACCEPTED: avatar-only later work; a later import's updates do not protect a created
  candidate from the creating import's undo.

## Piece 5 - candidate sourcing
Plan: docs/superpowers/plans/2026-07-29-candidate-sourcing.md (8 tasks)
P5 Task 1: complete (4cca6ad..3c1f0aa, 1104 passed, review clean)
P5 Task 2: complete (3c1f0aa..c74d1a8, 1135 passed, review clean after test fixes)
  CARRY TO TASK 3 (IMPORTANT): tokens() strips ALL punctuation, so "C++" -> "c" and collides
    with plain "C". Fine for job titles. WRONG for skills - use normalize_skill from
    candidate_naming.py:83 for skill comparison, NOT tokens()/overlap().
  Period basis is annual: 2080 hr, 260 day, 52 week, 12 month per year, commented.
P5 Task 3: complete (c74d1a8..2ac311a, 1155 passed). Fable 9/10 APPROVE; no edits needed here.
  BINDING DIRECTIVES FOR TASK 5 (from Fable):
   - [DONE - executed in Task 6, migration d2f6a41b8c73] widen sourcing_matches.score to
     Numeric(6,4) and persist 4 places;
     storing at 2dp would collapse distinct scores into ties on read-back.
   - order by (score DESC, candidate_id) - eligible.py already orders input by id.
   - eligible_candidates returns ids only: fetch roles+skills in ONE batched IN(...) query,
     not per candidate.
P5 Task 4: complete (2ac311a..59f5214, 1168 passed). Fable 9/10 APPROVE, no fix-now.
  Fable ran adversarial REPL probes: C++, M(F), A|B, .NET all escape correctly; the
  naive-sequential corruption case is real and the single-pass longest-first design prevents it.
  DEFERRED: re.IGNORECASE is simple case-mapping not casefold (theoretical for SG shorthand);
    a one-char protected code would shred prose - fix belongs at the glossary API min-length.
  CARRY TO TASK 5: redact must be called on title AND description AND requirements; the
    test must assert the code is absent from the WHOLE assembled prompt, not one field.

P5 cross-task review of Tasks 1-4 (Fable, 8.5/10 APPROVE WITH CHANGES). Nothing blocks 1-4.
  ADDITIONAL BINDING DIRECTIVES FOR TASK 5 - both would raise at runtime, not in tests:
   - Component carries Decimal fields (weight, raw, contribution). Decimal is NOT
     JSON-serializable, so writing components verbatim into sourcing_matches.reasons (JSONB)
     will throw at insert. Task 5 must define an explicit serializer (str or float per field).
   - score_candidate can return None, but sourcing_matches.score is NOT NULL. Task 5 must
     drop no-data candidates before insert; score.py's docstring says so, nothing enforces it.
  MINOR, deferred: "placed" is still a literal in api/candidates.py:38 StageFilter (Literal
    cannot reference Candidate.PLACED); SourcingRun.STATES duplicated as literals in the
    check constraint; models/__init__.py exports are out of alphabetical order and several
    candidate classes remain unexported (pre-existing, not worsened).
  NOTE: test-env.sh was NOT modified in this range - verified empty diff. It was the Piece 2
    environment fix, already ledgered above.
P5 Task 5: complete (46b903a..b02a24e, 1180 passed). Fable 8.5/10; §15 hole closed - evidence
  of exactly "Not mentioned" set is_missing, so verify() returned True VACUOUSLY and an
  unlocated quote reached the caller. _supported now also rejects is_missing / start_char None.
  Protected reports are unioned across passes (a second pass could erase the first's report).
  Explanations deduped by candidate_id.
  DEFERRED, ruled correct: no CV truncation cap (truncating would make honest quotes
    unverifiable); per-response escalation (per-candidate would multiply cost and latency).
  CARRY TO TASK 6: call explain_matches(opportunity, candidates: list[MatchCandidate], *,
    codes=<OpportunityCode rows>, llm=None) -> (list[Explanation], ProtectedReport).
    It sorts internally. Store the ProtectedReport on the run. Log assembled prompt length.
P5 Task 5 fix reviewed by Fable (9/10 APPROVE). The §15 fix closes the whole hole, not just
  the reported instance: is_missing is verify()'s ONLY vacuous-True path, empty/whitespace
  quotes are rejected upstream, and the guard uses "start_char is None" rather than a
  truthiness check - so a quote located at offset 0 correctly survives. That falsy-check
  mistake is the classic one in this exact fix and it was not made.
  MINOR, deferred: a duplicate candidate_id is skipped without setting fell_short, though a
    duplicate is itself a sign of a garbled response; union dedup is exact-string so a
    rephrased protected report survives twice (over-reporting is the safe direction).
P5 Task 6: complete (eaf5fc6..15b3756, 1190 passed). Fable 8.5/10 APPROVE WITH CHANGES -
  all five directives landed and Task 6's code is sound; the changes BIND TASK 7.

  THE CLIENT GAP - a hole in the spec I wrote, not a coding choice. The eligibility rule
  "not already submitted to this client" assumed a job order knows its client. IT DOES NOT:
  there is no opportunities.client_id. Task 6 infers it from client_mentions on the source
  email with a nil-UUID sentinel, which SILENTLY DISABLES the exclusion when unresolvable.

  TASK 7 MUST:
   1. Resolve the client at ENQUEUE time in the route, pass client_id explicitly to
      run_sourcing, and STORE it on sourcing_runs (new nullable client_id column, migration
      in Task 7). A run is a record; nothing currently records which client the exclusion
      used, or that it used none.
   2. When unresolvable: RUN ANYWAY BUT FLAG IT - surface "already-submitted exclusion could
      not be applied: no client on this job order" on the run and in the UI. Refusing kills
      the feature for every unmatched client; silence is the re-pitching embarrassment.
   3. Resolution must PREFER matched_by='domain' OVER name matches, and must not
      ORDER BY created_at LIMIT 1 - a name match is "a resemblance" (client.py:105), and an
      email mentioning two clients would otherwise pick arbitrarily and wrongly EXCLUDE
      candidates. ON DELETE SET NULL on retention purge also breaks the email join over
      time, which is a second reason to persist the resolved client rather than re-infer it.
   4. Enforce SOURCING_DAILY_RUN_QUOTA in the route, not the worker - worker-side would
      strand already-created run rows.
  MINOR deferred: run.model_name is set even when nothing was explained.

## PIECE 5 HANDOFF - read this first in a fresh session
  Plan: docs/superpowers/plans/2026-07-29-candidate-sourcing.md (8 tasks)
  Spec: docs/superpowers/specs/2026-07-29-candidate-sourcing-design.md
  DONE: Tasks 1-7.  REMAINING: Task 8 (UI) only.
  Baseline: 1207 passed, 1 skipped.  Alembic head: f4b8c1e7d290.
  Tests: cd backend && scripts/test-env.sh -q      (do NOT hand-roll env vars or copy CI's -
    CI uses a different app-role password and forcing it yields hundreds of bogus auth
    failures that look like flakiness.)  Also: uv run ruff check .
  Task 7 is bigger than the plan says - see the four TASK 7 MUST directives above.
  Nothing in piece 5 is pushed, and no piece 5 migration has been applied to Koyeb.
P5 Task 7: complete (5fe53f8..583bb65, 1207 passed). Fable 9/10 APPROVE, nothing to fix.
  The client hole is CLOSED: resolved in the route at enqueue, passed explicitly, stored on
  sourcing_runs.client_id; the nil-UUID sentinel is DELETED not bypassed; the sweep sends
  routing ids only and the worker falls back to the stored column.
  Resolution matches the real stored value 'email_domain' (NOT 'domain' as my directive said
  - a wrong string would have silently made every resolution fall back to unresolved).
  Domain beats name by set logic, no created_at tiebreak, ambiguity => unresolved and flagged.
  New alembic head: f4b8c1e7d290.
  DEFERRED by owner: a merged client is not chased to its survivor, so the exclusion applies
    to the loser row - a candidate submitted to the SURVIVOR can reappear (re-pitch risk),
    but nobody wrongly disappears. Wrong-inclusion is recoverable, wrong-exclusion is not,
    so deferral is safe. Own small task.
  MINOR deferred: the routing test reads app.openapi() paths, which lists a shadowed route
    too, so it does not itself prove non-shadowing (the functional HTTP tests do); quota
    check and insert are not atomic, so concurrent requests can slightly overshoot a daily
    soft quota.
  CARRY TO TASK 8 (UI):
   - a worker-failed run can have failure_reason NULL - do not assume every failed run has
     a sentence to show.
   - render BOTH client_id and client_unresolved_reason; an unresolved run must say the
     already-submitted exclusion could not be applied.
   - score arrives as a STRING, not a number.

### TASK 8 (UI) - everything it needs, so it need not re-derive any of it
  ROUTE COUNT: the plan says "six routes from Task 7". Task 7 shipped FIVE:
    POST /api/opportunities/{id}/sourcing, GET .../sourcing, GET .../sourcing/{run_id},
    POST /api/candidates/{id}/submissions, DELETE /api/candidates/{id}/submissions/{sid}.
  SHAPES (source of truth: backend/app/api/sourcing.py):
    run   = {id, opportunity_id, state, client_id, client_unresolved_reason,
             candidates_considered, shortlisted, protected_attribute_noticed,
             protected_attribute_note, failure_reason, created_at}
    state = pending | running | done | failed
    GET returns {run, matches}; run is NULL when there has never been one.
    match = {candidate_id, score (STRING), reasons, explanation, explanation_evidence}
  A MATCH CARRIES candidate_id ONLY - no name. The shortlist must join names from the
    candidates data the dashboard already loads. Do not expect them from these routes.
  SUBMISSION POST NEEDS client_id IN THE BODY. An unresolved run has no client_id, so
    "Mark submitted" must handle that case - disable it, or ask which client.
  failure_reason is NULL on a worker-failed run (only the route's enqueue-failure path
    writes one). Never render an empty error box.
  score is a STRING and ordering is already done server-side (score DESC, candidate_id) -
    never sort or compare it numerically in the UI.
  Poll only while state is pending or running. Styles go in frontend/app/app.css.

P5 Task 8: complete. PIECE 5 COMPLETE (4cca6ad..8fece81, 19 commits, 1208 passed).
  Fable final: SHIP WITH FIXES 8.5/10; both closed in 8fece81.
   - The shortlist was not short: the worker stored a match for EVERY scored candidate, the
     GET returned them all, and the UI fired one name request each. A 2,000-candidate agency
     meant 2,000 rows and 2,000 requests from one screen. Now capped by SOURCING_MAX_MATCHES
     (20) at the point matches are written; candidates_considered still records everyone
     scored, so "we looked at 2,000, here are the top 20" survives.
   - The safeguards copy claimed the requirement never reached the model. False for the
     plain-words case - those are precisely the ones the model itself reported. Now
     distinguishes coded (redacted first) from plainly-worded (seen, reported, ignored).
  ACCEPTED, not fixed: merged client not chased to its survivor (wrong-inclusion is
    recoverable, wrong-exclusion is not - own small task); model_name records the fast model
    after escalation; quota check is not atomic; protected-report dedup is exact-string;
    no CV truncation cap; per-response escalation; the routing test reads openapi().
  UNENFORCED INVARIANT: SOURCING_MAX_MATCHES (20) must stay >= SOURCING_EXPLAIN_TOP_N (10),
    or fewer candidates get explanations than the explain setting implies. No validator.
  NOT PUSHED. No piece 5 migration applied to Koyeb. Head: f4b8c1e7d290.
  HUMAN MUST CHECK ONCE SIGNED IN: (1) "Find candidates" on a real job order reaches done
    with a rendered breakdown; (2) a coded job order shows the protected-attribute notice;
    (3) an unresolved-client run shows its notice and "Mark submitted" is disabled with the
    explanation.
