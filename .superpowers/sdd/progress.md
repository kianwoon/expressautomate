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
P5 final fix reviewed by Fable (9/10 APPROVE). Cap sits immediately after the sort so it
  keeps the best, not a slice; candidates_considered still records the full population; the
  test is a genuine prefix check, not coincidence-passable; both safeguard copy branches are
  true against the code.
  VALIDATOR RULING - ACCEPT, do not add one. If SOURCING_MAX_MATCHES < SOURCING_EXPLAIN_TOP_N
  the behaviour degrades coherently (you cannot explain more than you keep); a cross-field
  validator would turn harmless config into a startup failure, which is worse. Add a one-line
  "must stay >=" comment on the setting next time that file is touched.
  MINOR accepted: when protected_attribute_noticed is set with no note, the banner shows but
  the recruiter cannot tell coded from plainly-worded. The copy omits the pointer rather than
  guessing, so it is honest - cosmetic only.

## Piece 6 - clients administration (add/edit/suspend/contacts)
Plan: docs/superpowers/plans/2026-07-30-clients-administration.md (7 tasks)
Spec: docs/superpowers/specs/2026-07-30-clients-administration-design.md
NOTE: the plan's stated baseline was stale. Real head before Task 1 was 6b1e9f4d7a20,
  suite already at 1421 passed. New alembic head after Task 1: a0bfc93f7eb8.
NOTE FOR EVERY LATER TASK: agency_with_clients yields a TUPLE, not an object with
  .confirmed_id - the plan's sample test code uses placeholder attribute names.
Task 1: complete (e46d0e3..9759a82, spec OK, quality approved). 1421 passed, 1 skipped.
  MINOR for final review: client_contacts has no touch_updated_at trigger, unlike
    clients/client_mentions (20260728_1100_client_profiles.py:134-146). ORM onupdate covers
    ORM writes, so not a bug today; a raw SQL UPDATE would leave updated_at stale.
Task 2: complete (9759a82..074c106, spec OK, quality approved, no findings). 1427 passed, 1 skipped.
Task 3: complete (074c106..ef68b1c, spec OK). 1437 passed, 1 skipped.
  Review (opus) found 4 Minors; 3 closed in ef68b1c: stale "no narrowing here" comment deleted;
  PATCH exclude_unset now pinned (an omitted field must not be nulled); PATCH cannot write
  status/source now pinned. The fixer caught its own vacuous assertion (source default IS
  'pipeline', so asserting != 'pipeline' proved nothing) and injected 'manual' instead.
  MINOR left for final review: PATCH email_domain:"" is coerced to a clear rather than 422 -
    defensible, undocumented, untested.
Task 4: complete (ef68b1c..a3e8903, spec OK, quality approved). 1442 passed, 1 skipped.
  PLAN BUG the implementer caught and fixed: tenant_session sets app.tenant_id with SET LOCAL,
    so it is TRANSACTION-scoped. The plan's commit-then-reread-in-the-same-session pattern
    silently 404s under RLS. Code now uses flush()/refresh() and lets the context manager
    commit. NEVER commit then re-read on the same session anywhere in this codebase.
  Review (opus) findings closed in a3e8903: PATCH contact {"name": null} wrote NULL into a
    NOT NULL column (500) - now 422 at the request model; _load_contact's client_id filter
    (client A's contact reached via client B's URL, same tenant) is now negatively tested.
  clients.py is 853 lines (cap 1500).
Task 5: complete (a3e8903..4e0031e, spec OK, quality approved). 1445 passed, 1 skipped.
  Matcher test passed on FIRST run, as designed - it pins existing indifference so a future
    _BY_DOMAIN change cannot silently break it. Reviewer confirmed it would fail if the
    matcher started skipping or rewriting suspended rows.
  Sourcing-still-runs test asserts 202, the route's real code (the plan guessed 200/201).
  MINORS for final review:
   - the suspension 409 and the pre-existing duplicate-submission 409 differ only by prose;
     no error code. TASK 7 must render detail verbatim, never a generic "already submitted".
   - the suspension check runs before the opportunity 404, so a suspended client plus a bad
     opportunity_id yields 409 rather than 404.
Task 6: complete (4e0031e..c3171e6, spec OK, quality approved, no findings). vitest 5/5, tsc clean.
  readError in clients.ts ALREADY preserves the server's `detail` - unchanged.
  Widening Client forced STATUS_LABEL maps in client-panel.tsx and clients-table.tsx to gain
    `suspended` (they are exhaustive Records, so tsc caught it).
  CARRY TO TASK 7: two stale docstrings say "there is no create form here" - now false.
Task 7: complete (c3171e6..f5ca951, spec OK). tsc clean, build OK, vitest 16 passed.
  DEVIATION UPHELD by review: the free-provider 422 comes from a pydantic field_validator and
    backend/app/main.py has NO RequestValidationError handler, so detail is a LIST, not a
    string. Task 6's readError would have rendered "[object Object]". clients.ts gained
    readProblem + FieldError; the string branch (all the 409s) is byte-identical.
  DEVIATION UPHELD: frontend/app/app.css was ALREADY 1507 lines (pre-existing, over the 1500
    cap), so new styles went in dashboard/clients/clients.css. app.css untouched.
  Review (opus) findings closed in f5ca951: Cancel after a PARTIAL create (client POSTed,
    contact call failed) left a real client invisible in the list - now reloads and selects
    it; fromValidationEntries no longer assumes index 0 and no longer drops later messages.
  Submissions 409 verified by hand at job-orders-sourcing.tsx:194 - renders err.message
    verbatim, so the suspension reason reaches the recruiter.
ALL 7 TASKS COMPLETE. Backend 1445 passed, 1 skipped (verified by controller, not reported).
FINAL REVIEW (fable): SHIP WITH FIXES 8.5/10. One cross-task seam - merge left a suspended
  loser's suspended_at/suspended_reason behind, so unmerge produced an `unconfirmed` row still
  carrying a suspension reason. Closed in 4229dd4. 1446 passed, 1 skipped.
  Tenancy (§18) judged CLOSED: policy + composite FK + _load-before-_load_contact on every
  contact endpoint + same-tenant-wrong-client and cross-tenant 404s all tested.
  ACCEPTED AS-IS (fable triaged all five as fine to ship): no touch_updated_at trigger on
  client_contacts; PATCH email_domain:"" clears rather than 422s; the two 409s differ only by
  prose; 409-before-404 on suspended+bad opportunity_id; app.css 1507 lines is PRE-EXISTING
  debt over the repo's own 1500 cap - own follow-up task.
  KNOWN, ACCEPTED: update_client checks MERGED then updates without a row lock, so a
  concurrent merge can let a PATCH land on a just-merged row (pre-existing pattern).
NOT PUSHED. Migration a0bfc93f7eb8 NOT applied to Koyeb.
HUMAN MUST CHECK ONCE SIGNED IN (nobody has run the UI - OAuth blocks automation):
  (1) add a client end-to-end, including the partial-create recovery path (cancel after the
      client POSTed but a contact call failed - the row must appear, not vanish);
  (2) the duplicate-domain 409 names the holding client, and the free-provider 422 lands
      inline on the domain field (that is the readProblem list branch, untested against a
      real server);
  (3) suspend a client, then try a submission - the refusal must carry the typed reason.
Fix 4229dd4 reviewed by fable: APPROVE, no findings. _CLEAR_SUSPENSION has no mutation hazard (** and dict() at every call site); merge's locking/mention logic byte-identical; the test asserts BOTH the merge and unmerge halves.

## Piece 7 - client logos
Plan: docs/superpowers/plans/2026-07-30-client-logos.md (4 tasks)
Spec: docs/superpowers/specs/2026-07-30-client-logos-design.md
Logo Task 1: complete (5821b2a..8aaa789, spec OK, quality approved, no findings). 1447 passed.
  New alembic head 8c7e0f3c5305. NOT applied to Koyeb yet.
  LEARNED: tests/test_deployment.py::test_every_setting_is_discoverable_in_the_env_example
    fails for ANY new Settings field missing from the root .env.example. Every task that adds
    a setting must update .env.example in the same commit.
Logo Task 2: complete (8aaa789..7d1c5e0, spec OK, quality approved). 1458 passed, 1 skipped.
  All four safety properties verified IN CODE by the reviewer: limit+1 read before Pillow;
    Image.open header-first with DecompressionBombError -> 400; tenant check before the bytes
    are read; delete object THEN null columns. GET recomputes the key rather than signing
    whatever the row holds.
  Containment is real and the test would fail against a centre-crop (a crop leaves no
    transparent row). Resize path was UNTESTED (every fixture sat under the 1024 bound) -
    closed in 7d1c5e0 with an over-bound test (aspect ratio preserved, long edge capped) and
    a no-upscale test.
  Corrected an inherited inaccurate docstring: the limit+1 read protects decode CPU and
    memory, NOT disk - Starlette spools the whole multipart body before the endpoint runs.
  MINORS for final review, ALL INHERITED from candidates_avatar.py, none introduced here:
   - R2 put succeeding then the DB UPDATE failing leaves an orphan object (key is
     deterministic so a re-upload overwrites it, but nothing sweeps it).
   - deleting a CLIENT does not delete its R2 object. Same gap as deleting a candidate.
     The {tenant_id}/ prefix still makes a tenant purge possible.
Logo Task 3: complete (7d1c5e0..feb8a4b, spec OK, quality approved, no findings). tsc clean,
  vitest 20 passed, build OK. clients.css now 246 lines; app.css untouched.
  Reviewer verified: border-radius 12px not 50% (a circle would re-crop the wordmark the
    backend letterboxed); :focus-within on the overlay so it is keyboard-reachable; the file
    input is clip-rect hidden, NOT display:none, so it stays tab-reachable; the presign lives
    only in useState keyed on [id, logo_key, logo_updated_at] with a `cancelled` flag, so a
    stale response cannot land after the id changes.
  DEVIATION UPHELD: when logo_key is null the component skips the GET entirely rather than
    firing a request that would 404. The brief's own test requires it.
Logo Task 4: complete (feb8a4b..82b9588, spec OK, quality approved, no findings). tsc clean,
  vitest 22 passed, build OK.
  DESIGN CALL UPHELD: client-logo.tsx gained a `readOnly` prop rather than a second component.
    readOnly returns a DIFFERENT JSX tree with NO file input at all - not a hidden one, which
    would still be tab-reachable and still uploadable. The panel's interactive path is
    unchanged (onChange is now optional, called via onChange?.()).
  The client fetch is keyed on clientId in its own effect, separate from the poll effect, so
    it does NOT re-fire per poll tick while a run is pending/running.
ALL 4 LOGO TASKS COMPLETE. Backend 1458 passed, 1 skipped; frontend 22 passed (controller-run).
FINAL REVIEW (fable): SHIP WITH FIXES 8/10. Tenant isolation judged solid. TWO SECURITY
  findings, both INHERITED by candidates_avatar.py (already live in production) - fixed in
  BOTH files in a94053c:
   - REACHABLE OOM: Pillow's DecompressionBombError only fires above ~179Mpx, so a tiny PNG
     declaring 120Mpx passed Image.open with a warning and image.load() then allocated
     hundreds of MB - enough to OOM-kill api on a small Koyeb instance, from any recruiter
     session, repeatedly. Now image.size is checked against settings.IMAGE_DECODE_MAX_PIXELS
     (default 30Mpx) BETWEEN open and load, where the header is read but no buffer allocated.
   - NO FORMAT ALLOWLIST: Image.open tried every Pillow plugin including EPS, which shells out
     to ghostscript when installed - a historical RCE surface. Now pinned to
     _ALLOWED_FORMATS = PNG/JPEG/GIF/WEBP/BMP/ICO, a module constant not a setting, so it can
     never be widened from .env.
  1464 passed, 1 skipped.
  CACHE-BUSTING CORRECTION: the spec says logo_updated_at busts the browser cache. It does not,
    quite - it re-triggers a fresh presign, and the bust is that each presigned URL carries a
    new X-Amz-Date and signature so the browser never reuses one. Correct in practice.
  ACCEPTED AS-IS (all three inherited, fable triaged all as fine to ship): orphan object when
    the R2 put succeeds and the DB UPDATE fails (deterministic key self-heals on re-upload);
    deleting a client leaves its R2 object (covered by the documented tenant-prefix purge
    convention); Starlette spools the multipart body to disk before the size check.
  MINOR accepted: the upload path presigns twice (the component fetches a URL, then onChange
    refetches and moves logo_updated_at, refiring the effect). Harmless waste.
  TABLE EXCLUSION: reasoning judged sound as a deferral. The cheap option it missed, for
    whoever designs the table view: a 302-redirect endpoint with Cache-Control: private.
NOT PUSHED. Migration 8c7e0f3c5305 NOT applied to Koyeb.
HUMAN MUST CHECK ONCE SIGNED IN: (1) upload, replace and remove a logo - focus ring and camera
  overlay included; (2) a real WIDE wordmark renders letterboxed, not cropped, on both the
  panel and the sourcing screen; (3) the sourcing screen names the client instead of a UUID,
  and an unresolved run still shows its notice with no logo.
Security fix reviewed independently (fable, 8/10 SHIP WITH FIXES). Both closed in e6708eb:
 - THE ORDERING TEST PROVED NOTHING. test_a_canvas_over_the_decode_budget_is_refused_without_
   allocating stayed green if the guard moved AFTER load(): Pillow would allocate ~90MB, then
   the truncated IDAT raised OSError -> the same 400, store still empty. It now monkeypatches
   PIL.ImageFile.ImageFile.load to raise if called, so moving the guard fails loudly.
 - An unlisted-but-real format (TIFF, iPhone HEIC) said "That file is not a readable image",
   which reads as "your file is corrupt". UnidentifiedImageError now names what IS accepted,
   derived from _ALLOWED_FORMATS so the two lists cannot drift; OSError/ValueError keep the
   malformed-bytes message.
 - IMPLEMENTER'S FINDING: with the allowlist applied, plain garbage ALSO raises
   UnidentifiedImageError - Pillow cannot tell "wrong format" from "no format". So the
   corrupt-bytes test uses a valid PNG header with a truncated body, which parses at open()
   and fails in load() with a real OSError.
 CONFIRMED BY REVIEW: load() decodes frame 0 only (no seek), so animated GIF/WEBP and
   multi-size ICO stay bounded by the declared canvas; formats= is real plugin-dispatch
   prevention in the pinned Pillow 12.3.0 (uv.lock), so EPS/ghostscript is unreachable.
 IMAGE_DECODE_MAX_PIXELS STAYS 30M: worst case ~120MB RGBA (~240MB for 16-bit PNG), clears
   24Mpx iPhone defaults, and 48Mpx exports are mostly stopped by the 5MB gate anyway.
   Only TIFF is a real behavioural regression, and it is deliberate.
 CONCURRENCY IS ONLY SOFTLY BOUNDED - the cap is per request, not per process. A human should
   check the api instance's RAM against two concurrent worst-case decodes.
 1466 passed, 1 skipped.

## Gateway flake fix (93dfbf1) - reviewed, SHIP 9/10
ROOT CAUSE was the TEST, not production. The test emitted the QR on a setTimeout(20ms) that
  raced two DB round trips: pair() -> #openOnce awaits ensureWaSessionRow (sessions.ts:496)
  and usePostgresAuthState (:497, decrypts creds) BEFORE the socket exists (:532) and the
  connection.update handler attaches (:547-549). The fake drops events with no handler and
  never replays, so under CI load the QR vanished, firstSettled never resolved, and pair()
  waited its full 2000ms -> qr null. CI took 2249ms = 2000 + overhead. Exact signature.
PRODUCTION IS NOT AFFECTED and sessions.ts is byte-identical to origin/main (verified): the
  socket does not exist during those DB awaits so nothing can emit into that window, and
  there is no await between socketFactory() returning and ev.on() attaching.
FIX: fake.subscribed(event) resolves when the handler attaches (and immediately if it already
  has), then a setImmediate yield, then the emit. No wall clock anywhere.
  The setImmediate is NOT decoration: subscribed() resolves one promise-hop earlier than
  pair()'s own continuation, so without it a BROKEN pair() passed on microtask-ordering luck.
  Fable traced it independently: after :547 the path to the Promise.race is pure sync +
  microtasks with no timers or I/O, and Node drains microtasks to exhaustion before any
  setImmediate - so the emit ALWAYS lands with pair() already waiting. Guaranteed, not usually.
MUTATION-TESTED: bypassing the wait in pair() fails both dependent tests; restoring it passes
  82/82. 20 consecutive runs, 82/82 each, zero failures.
KNOWN, ACCEPTED: subscribed() for an event that never gets a handler dangles until the test
  timeout; the sibling test's <1000ms upper bound includes the two DB round trips (pre-existing,
  theoretical); the two setTimeout(50) waits at sessions.test.ts:322 and :680 race a
  fire-and-forget saveCreds() write - the file's remaining timing dependency, and the next
  thing here that will flake. A saveCreds hook or poll would fix them.

## Client logo reload bug (user-reported from the live app) - 74f991b, then 0f192c6
THE LESSON, and it is the important part: the spec and plan BOTH said "mirror
  candidate-avatar.tsx", and the implementation did not. candidates/page.tsx already had the
  answer - a refetchDetail/refreshDetail SPLIT - and three separate reviews passed over the
  client logo work without noticing the wiring diverged from the reference it named.
  When a plan says "mirror X", a reviewer must diff against X, not just read the new code.
THE BUG: ClientLogo's onChange was wired to the panel's generic onChanged -> refreshDetail,
  which did reload() (the WHOLE clients list, which does not even draw logos) AND getClient().
  The refetched detail carried a new logo_updated_at, re-firing ClientLogo's effect, blanking
  the image and issuing a THIRD presign. Three redundant requests and a full list re-render,
  which the user reasonably read as a page reload.
FIRST FIX 74f991b WAS WRONG-SHAPED: it dropped the notification entirely. Fable called it
  "exactly the third variant you said you didn't want" (6/10) and also caught that its test
  was theater - a `reload = vi.fn()` never passed to anything, so the assertion COULD NOT FAIL.
CORRECT FIX 0f192c6, now verified at parity (fable 9/10, SHIP): clients/page.tsx:143-156
  reproduces candidates/page.tsx:218-230 - refetchDetail is detail-only and is what the logo
  calls; refreshDetail is reload() + refetchDetail() and stays with confirm/archive/restore/
  suspend/edit/merge, the actions that change what the LIST draws.
  No flash: the shownFor guard (client-logo.tsx:94-99) resets to "loading" only on an ID
  change, so a same-id re-read swaps src silently - the same mechanism candidates uses.
  ONE JUSTIFIED DIVERGENCE: ClientLogo.onChanged is optional where CandidateAvatar's is
  required, because ClientLogo is also used readOnly on job-orders-sourcing.tsx:320.
  MUTATION-CHECKED: rewiring the logo back to onChanged makes the new panel test fail.
MINOR left: client-panel.test.tsx:315-340 is near-tautological - onArchive is a mock that
  itself calls onChanged, so only the onDetailChanged.not.toHaveBeenCalled() half carries.
Task 8: complete (b77ffc0..3542e96, review clean after 1 fix round, 1533 passed). opportunities.py 952 LOC.
  OUT-OF-BRIEF FIX (justified): list_opportunities had an INNER join on email_messages, so a
  hand-typed job order (email_message_id IS NULL) would never have appeared in the list - and
  nor would any row whose source email retention had purged. Now isouter=True + regression test.
  Both the race test and the join fix are mutation-proven by the reviewer.
  Cross-tenant assign returns 422 (RLS scopes the user lookup, so the target is simply not found).
Task 9: complete (3542e96..ff2666b, review clean, 1547 passed). clients.py 975 LOC (no split needed).
  OPEN PRODUCT QUESTION for the user: /clients/{id}/assignee and the collaborator routes use
  plain _require_session - ANY recruiter in the agency can reassign any client. Spec never said.
  Reviewer flagged as informational, not a defect. Decide before merge.
Task 10: complete (ff2666b..4e8c0c9, review clean, 1553 passed). ALL TASKS DONE.
  Idempotency mutation-proven: making the insert an upsert fails the replay test.
  MINOR for final review: merge-chain assignee (survivor's recruiter wins) is disclosed
  behaviour with no direct assertion in tests/test_client_matching.py.
FINAL REVIEW round 2: READY WITH FIXES - guard test still missed route->module-level-helper
  delegation (the original sourcing.py bug shape), proven by probe. Hardened transitively in
  5003c4e; probe now caught, no true positives, no exemptions added. 1564 passed.
FEATURE COMPLETE, NOT MERGED. Open: (1) should client reassignment be owner-only? (2) migrations
  not yet applied to live Koyeb DB - user chose live, deferred to one deliberate pass.
CLIENT PERMS (user decision): owner OR current assignee; unassigned client claimable by any
  recruiter; else 403. Implemented 2d94732, mutation-proven, 1571 passed.
BACKFILL DEFECT FOUND ON LIVE DATA (8322180): client_mentions is EVIDENCE not a key - one email
  legitimately names many clients. Live: 5 of 8 matchable opportunities had SIX candidate clients;
  matched_by='email_domain' did NOT disambiguate. Original UPDATE..FROM would have picked an
  arbitrary client, and client_id drives assignment. Now HAVING count(DISTINCT client_id)=1,
  ambiguity -> NULL (CLAUDE.md no-fabrication rule). Pinned by tests/test_opportunity_client_backfill.py.
PRODUCTION MIGRATED 2026-07-31: 8c7e0f3c5305 -> 314cc3da9ced (5 revisions). Verified live:
  client_id 3/11 (matches prediction), assigned_user_id 0/11, 11 opportunities intact,
  RLS enabled+forced on both new tables, all 3 composite FKs column-qualified.
  Local .env still points at the docker container; prod URL was passed per-command only.
FABLE ② REVIEW of the post-migration tail (2d94732, 8322180): SHIP, 8/10, no Criticals.
  IMPORTANT fixed in d35d594: TOCTOU - permission check read the client's assignee without a row
  lock, so a just-deposed assignee's in-flight PUT could still move the outgoing recruiter's whole
  book. Now one FOR UPDATE read serves both the check and `previous`; deterministic lock test in
  tests/test_client_concurrency.py (fails without the lock: 200 != 403).
  MINOR fixed: merged clients now refused (400 "Unmerge the client first"), matching update_client.
  Archived still allowed, matching update_client. 1578 passed.
FINAL STATE: 28 commits, 1578 passed, ruff clean, single head 314cc3da9ced. Production migrated.
  Branch NOT merged, code NOT deployed.

## Job order sharing UI
Plan: docs/superpowers/plans/2026-07-31-job-order-sharing-ui.md
Branch: kianwoon/job-order-sharing-ui-a8ce83  Base: c121d03
Worktree: .claude/worktrees/job-order-sharing-ui-a8ce83 (main checkout .env points at PROD - never test there)
ENV: worktree .env -> localhost:5432 docker `ea-test-pg`; DATABASE_URL=app role, ADMIN=postgres.
Baseline: backend 1578 passed, frontend 24 passed (7 files). Alembic head 314cc3da9ced.
NOTE: no @testing-library/jest-dom or user-event installed, no setupFiles. House-style asserts only.
UI Task 1: complete (c121d03..4020846, review clean after 1 minor fix, 1584 passed)
  Payload gains assigned_user_id/assignee_name/client_id/source/shared_with_me.
  LEFT join (an inner one would have dropped the whole queue); join now composite on tenant too.
  NOTE: the cross-tenant test is a regression guard only - RLS blocks it either way, so it
  cannot distinguish composite from bare join. Implementer said so rather than overclaiming.
UI Task 2: complete (4020846..7887fb0, review clean, 1589 passed). GET /api/members.
  Controller resolved the reviewer's one warning: seed_tenant_with_user inserts only
  id/tenant_id/email/role, so both name columns really are NULL - the fallback test is valid.
UI Task 3: complete (7887fb0..219a159, review clean after 1 minor fix, 1596 passed). opportunities.py 1029 LOC.
  ?scope= ANDed with the predicate, never substituted - mutation-proven (dropping .where(visible)
  fails test_no_scope_can_widen_visibility). _shared_with_me_exists shared by payload + filter.
  HONEST LIMIT recorded in the test: only the `all` leg can discriminate. mine/queue/shared_with_me
  clauses are verbatim OR-branches OF the predicate, so no row can match a scope yet be hidden.
=== PHASE A (backend) COMPLETE. Frontend next. ===
UI Task 4: complete (219a159..0cbff81, review clean, 34 frontend tests / 9 files). person.tsx + members.ts.
  Client logo colours PROVABLY unchanged: indices 7/4/7 pinned and independently recomputed by the reviewer.
  Implementer's own test caught a real bug: cache = pending.catch(...) stored the DERIVED promise, so
  the cache===pending guard never fired and a failed fetch cached forever (picker dead for the tab).
  resetMembers() wired into signOut() in site-nav.tsx (verified the only LOGOUT_PATH caller).
  FOLLOW-UP: candidate-avatar.tsx holds a third copy of colorFor/initialsFor - deferred, different screen.
UI Task 5: complete (0cbff81..2cc2aaf, review clean, 50 tests / 10 files). MemberPicker + MemberSelect.
  Caller excluded via useAuth() INTERNALLY (no call site can forget). Disabled while auth loads, so
  the caller never flickers into the list. Keyboard: Arrow/Enter/Escape mirroring dialog.tsx.
  FOR TASKS 9/10: (a) MemberSelect renders a labelled "someone who has left" option when `value`
  names an id absent from the staff list - stops a save silently meaning "nobody". (b) options
  commit on MOUSEDOWN, so consumer tests must use fireEvent.mouseDown, not click.
UI Task 5b: app.css split (user decision) - it was 1560 BEFORE this feature (bar is 1500), 1672 after.
UI Task 5b: complete (2cc2aaf..0a17196). app.css 1672 -> 1283 (UNDER the 1500 bar); new
  app/dashboard/job-orders.css = 440. npm run build passed (21/21 pages), 50 tests unchanged.
  Shared primitives (.jo-table/.jo-chip/.person-initials/.mp-*) deliberately LEFT in app.css -
  moving them would have broken the candidates and clients screens. New job-order-only CSS in
  Tasks 7/9/10 goes in job-orders.css.
UI Task 6: complete (0a17196..d628859, review clean, 60 tests / 11 files). opportunities.ts 679 LOC.
  409/404/403/401 kept as DISTINCT sentences; conflict:true only on 409. Scope change resets offset.
  allow-hardcode: annotation on the status->message map judged legitimate (statuses are the logic).
  NOTE: opportunities.ts is near the ~700 split threshold - next addition there should split out.
UI Task 7: complete (d628859..4e678b8, review clean, 65 tests / 12 files, build passed).
  Table still exactly 8 columnheaders; avatar inside the company cell. Both chip rows COMBINE
  (status + scope both reach the same request). job-orders.css 440->458; app.css untouched at 1283.
  MINOR/UX for final review: both chip rows contain a chip labelled "All" - distinguished only by
  group aria-label. Sighted users see "All ... All". Worth a look in the real app.
  ALSO: implementer asked for an eyeball on the scope row's placement below .jo-controls.
UI Task 8: complete (4e678b8..2988eed, review clean after 1 fix round, 74 tests / 14 files, build passed).
  REAL BUG CAUGHT: claiming visibly SNAPPED BACK - own() set the fresh row but a sync effect found
  the stale object still in items and overwrote it. Fixed with patchRow(fresh); mutation-verified
  (removing that line fails job-orders-claim.test.tsx).
  MutationResult now carries kind: conflict|gone|forbidden|denied|failed. No control flow anywhere
  branches on message TEXT any more; GONE_MESSAGE/FORBIDDEN_MESSAGE deleted.
  403 copy depends on the ROW: "shared with you, not assigned" vs "claim it first" when unassigned.
=== PHASE C COMPLETE - assignment loop usable. Sharing next. ===
UI Task 9: complete (2988eed..53e7fa6, review clean, 83 tests / 15 files, build passed). share-dialog.tsx.
  Broadcast gate MUTATION-VERIFIED (loosening it fails 2 of 9 tests) and mirrors the API exactly.
  Disabled-with-reason, two distinct reasons. No access level invented. job-orders.css 515; app.css 1283.
  MINORS carried: (1) the `assigned_user_id !== null` clause is unreachable so no test protects it;
  (2) no frontend test pins a repeat share - CONTROLLER RESOLVED: the backend already pins it in
  test_resharing_the_same_user_in_a_fresh_request_updates_the_note (loops 6 upserts for the generic plan).
=== PHASE D COMPLETE. Clients + manual creation next. ===
UI Task 10 + 10b: complete (53e7fa6..8de162b, review clean after 2 fix rounds).
  Backend 1604 passed, frontend 92 / 16 files, build passed. client-assignee.tsx 235 LOC (split out).
  FOURTH API GAP the spec missed, fixed in 967f900: clients _serialize emitted NO assignee, so every
  client would have read as unassigned in production. Also added collaborators to the DETAIL payload
  only (list stays single-query).
  MemberSelect now INCLUDES the caller (you must be able to assign a client to yourself);
  MemberPicker still excludes. Collaborator picker passes a local exclude.
  Whitespace-only name now falls through in opportunities.py too (8de162b), mutation-verified.
  *** CARRY TO FINAL REVIEW: the email fallback still DIFFERS - clients.py yields the local-part
  ("raj"), opportunities.py yields the full email ("raj@agency.sg"). Same person, two screens,
  two names. Originated in the spec. Decide one and make both match. ***
UI Task 11: complete (8de162b..e75055d, review approved, frontend 97/17, backend 1605, build passed).
  FIFTH API GAP: GET /api/clients had NO q param - the search test would have passed against an
  endpoint silently ignoring the query. Added + mutation-verified.
  addRow added (patchRow no-ops for a row not already on the page). Mutations split to
  opportunity-actions.ts (164), re-exported so no caller changed.
  CARRY TO FINAL REVIEW: (1) addRow ignores current sort/scope/filter/q - prepends and bumps total
  even when the server would not return the row (transient, self-corrects on poll).
  (2) CLIENT_SEARCH_DEBOUNCE_MS is a second literal, not the shared constant. (3) no test pins the debounce.
=== ALL 11 TASKS COMPLETE ===
FINAL REVIEW: READY WITH FIXES. 3 findings raised; controller REJECTED 2 as wrong:
  - "Share button ungated" - WRONG. The 403 at opportunity_shares.py:68 is INSIDE
    `if body.scope == SCOPE_TENANT:` and gates broadcast only. Named sharing is deliberately
    open to anyone who can see the row (design: work finds the right person through a chain).
  - "no cross-tenant test for /api/members" - WRONG. test_it_never_lists_another_agency exists.
  GENUINE finding fixed in 99e3930: shared_with_me EXISTS was written TWICE (visibility.py and
  opportunities.py). Extracted to visibility.py::shared_with_me_exists; inverting it now fails
  BOTH a visibility test and a scope test.
  Also fixed: email fallback now local-part in all 3 places (spec updated); chips relabelled
  "All job orders" / "Everyone".
  ACCEPTED as cosmetic: addRow ignores sort/filter (self-corrects on poll); duplicate debounce
  literal; no debounce test; unreachable null clause; candidate-avatar.tsx third helper copy.
FINAL: backend 1606 passed + ruff clean; frontend 97 passed / 17 files; build passed.
  All files under 1500. FEATURE COMPLETE, NOT MERGED.
FABLE INDEPENDENT REVIEW: READY TO MERGE. Confirmed BOTH controller rejections were correct.
  3 low findings, none blocking, all pre-existing at base:
  (1) create_opportunity inserts body.client_id unchecked -> a stale/cross-tenant id gives a 500
      from the composite FK (no leak; assign_opportunity pre-checks its target, this does not).
      Same class in the named-share POST loop. The UI now exposes both paths.
  (2) test_opportunity_routes_guarded.py:76 exemption comment calls client reassignment an
      "undecided product question" - it was decided and shipped in 2d94732. False comment.
  (3) no cross-tenant-target test for the share POST.
MERGED + DEPLOYED 2026-07-31: main 23dafdf -> b864f6e (21 commits, fast-forward).
  First deploy attempt FAILED at "Deploy api": Koyeb timed out contacting ghcr.io while validating
  the image. Transient infra, not code - build+push had succeeded and the tag existed in ghcr.
  No migration was pending, so production stayed on the old build throughout (health 200 the whole time).
  `gh run rerun <id> --failed` succeeded. Verified live: /api/members, ?scope=, ?q= all 401
  (present + authenticating) vs 404 for a nonexistent route.

## Linking a job order to its client
Plan: docs/superpowers/plans/2026-07-31-linking-a-job-order-to-its-client.md
Branch: kianwoon/job-order-client-link-b0decc  Base: 827bc0f
Worktree: .claude/worktrees/job-order-client-link-b0decc (MAIN checkout .env is PROD - never test there)
Baseline: backend 1606, frontend 97/17. Alembic head 314cc3da9ced. No migration in this plan.
WHY: 8 of 11 production job orders have client_id IS NULL (5 whose email named SIX clients, 3 that
named none), so neither ingestion nor client reassignment can ever route them.
CL Task 1: complete (827bc0f..e634130, review clean after 1 minor fix, 1618 passed). opportunities.py 1142 LOC.
  POST /api/opportunities/{id}/client. Permission "unassigned OR editable" - MUTATION-VERIFIED
  (adding `or True` fails the bystander test, which also asserts no client was written).
  Adoption gate mutation-verified too (removing the unassigned check fails the never-changes-hands test).
  Emits EVENT_OPPORTUNITY_ASSIGNED after commit, naming only the new owner; no-op link emits nothing.
  Guard test passed with NO exemption, as the plan predicted.
  _assignee_name_expr extracted to module level, shared with list_opportunities; whitespace fallback
  now pinned by a mutation-proven test (plain coalesce fails it).
CL Task 2: complete (e634130..ea96809, review clean, 102 tests / 18 files, build passed).
  client-search.tsx (151) extracted; job-order-form.tsx 325 -> 194; its tests passed UNCHANGED.
  Debounce test now COUNTS requests - a per-keystroke implementation fails it. Useful finding:
  mutating the delay to 0 does NOT fail it (synchronous fireEvents + cleanup clears a 0ms timer),
  so future reviews must mutate by REMOVING the timer, not by tweaking the constant.
  ACCEPTED scope addition: error surfacing on a failed search (the original was silently no-op and
  left stale matches on screen). Reviewer endorsed; the brief's own 5th test asked for it.
  FOR TASK 3: the hint copy is hardcoded and names the manual form's field ("The name still goes in
  Company") - it will read oddly in the panel. Task 3 owns adding a `hint` prop.
CL Task 3: complete (ea96809..f781326, review approved, 110 tests / 19 files, build passed).
  patchRow+setSelected both called - MUTATION-VERIFIED (removing patchRow fails 2 tests), so the
  snap-back bug from the previous feature is not repeated.
  Test-first caught a real silent bug: a stray setNotice meant 403s in the client field rendered
  NOTHING while the 404 path still passed. Mutation-verified that the new test is what catches it.
  hint prop added to ClientSearch, default byte-identical to the old copy.
  IMPORTANT finding -> CL Task 4: a linked row does not say WHICH client. Opportunity carries
  client_id but no name, so the picker cannot pre-fill and the only "linked" signal is the ABSENCE
  of the unlinked sentence. After remediating the 8 rows nobody could audit what they linked.
  MINORS carried: two `Client` labels co-exist (dialog is aria-modal so AT sees one); the shared
  `moving` flag makes claim and link disable each other.
CL Task 4: complete (f781326..5a60582, review clean NO findings, backend 1623, frontend 113/19, build passed).
  client_name on the payload + link response. LEFT composite join MUTATION-VERIFIED (isouter=False
  fails 2 tests) - an INNER join would have hidden 8 of 11 production rows.
=== LIVE PRODUCTION BUG FOUND (pre-existing, shipped in the previous feature) ===
  The backend has NO GET or PATCH on /api/opportunities/{id}. Confirmed against production:
  GET /api/opportunities/<uuid> -> 404 while GET /api/opportunities -> 401.
  (1) updateOpportunityPlacement PATCHes that route; PlacementForm (detail-panel.tsx:10) calls it.
      The placement form is BROKEN in production - and placement_type is the regulatory control
      that unlocks the lawful sex filter. Backend exposes POST /placement-type and
      POST /occupational-requirement instead.
  (2) getOpportunity GETs the same route, swallowed by `catch { next poll }`, so every read-back
      after claim/assign/link/create silently fails - including the patchRow snap-back fix.
  WHY NO TEST CAUGHT IT: fixtures stub fetch to answer ANY url, so a call to a nonexistent route
  looks like success. Same blind spot class as the two cross-recruiter leaks in the last feature.
  USER DECISION: fix both on this branch + add a contract test over api.ts path helpers.
CL Task 5: complete (5a60582..4d23459, review approved after 1 minor fix).
  Placement form now makes TWO calls (each backend route stamps its own audited set_by/set_at;
  one combined write would record a lawful sex-requirement judgement against someone who only
  picked a permit type). Sends only the half that changed; refusal shown last; row re-read either way.
  GET /api/opportunities/{id} added, sharing _row_select with the list - drift caught by a full
  payload == comparison, not a key list.
  THIRD BUG the contract test surfaced: _payload never returned placement_type/sex_requirement/
  sex_requirement_reason though the frontend type declared them, so the form drew "not set" over a
  SET placement type - the field gating the lawful sex filter. Fixed for list and single read.
  Contract test: one it.each per api.ts helper vs the backend's emitted route table. Mutation-verified
  twice (implementer and reviewer, different helpers). Cannot check HTTP verbs - documented.
  Form now resyncs its selects from the read-back, so a refused value is not left looking saved.
  MINOR carried: the contract test's skip rule uses text.includes over app/**; a future barrel
  re-export or computed helper name would silently disable a check.
=== ALL TASKS COMPLETE ===
CL Task 6: complete (4d23459..5979947). LOST-CLAIM RACE fixed then the 409 restored.
  (a) set_opportunity_client wrote assigned_user_id unconditionally off a stale read, so linking a
      client could silently overwrite a colleague's claim - even with adopt=false. Now client_id is
      written on its own and adoption is a separate CAS (WHERE assigned_user_id IS NULL).
  (b) The claim loser was getting 404, not 409: load_visible_opportunity ran FIRST, and the winner's
      claim makes the row invisible to the loser. Reordered to CAS-first. Fable verified the reorder
      is safe (unassigned is the FIRST disjunct of the predicate, and RLS gates UPDATE row selection).
  ACCEPTED TRADEOFF, Fable-reviewed: a same-agency colleague holding an id now learns an assigned row
  exists (409 not 404). The SPEC CONTRADICTED ITSELF - it mandates 409 for the claim loser AND a
  blanket 404 rule, and the loser and a bystander are the same DB state, so no query honours both.
  test_claiming_a_job_order_you_cannot_see_is_a_404 rewritten; cross-agency still 404 and pinned.
=== ALL COMPLETE. Final: backend 1636, frontend 190/21. ===

## Piece 3 — Candidate ownership and sharing
Plan: docs/superpowers/plans/2026-07-31-candidate-ownership-and-sharing.md
Spec: docs/superpowers/specs/2026-07-31-candidate-ownership-and-sharing-design.md
Branch: kianwoon/recruiter-candidate-sharing-083e1e  Base: 5a9c865
Pre-flight decisions (user, batched before execution):
  - Task 6: FULL delivery path. Teach deliver_notification + render.py about candidate
    events; add the six kinds to ALL_EVENT_KINDS. NOT emit-only.
  - Task 7: COPY test_opportunity_routes_guarded.py verbatim. Duplication is deliberate —
    two independent copies cannot both drift on one bug. Tell reviewers this.
Tasks: 0..15 (16 total).
Task 0: complete (5a9c865..aa70765, review clean — spec OK, 3 Minor only).
  Moved client/seeded/sign_in to conftest + make_candidate/make_user/run_import.
  Also fixed 7 opportunity test files that aliased the fixtures off the module (necessary,
  imports only, no behaviour change — reviewer confirmed).
  Brief was WRONG twice, implementer used source: CandidateRecord is in
  imports/rows.py (not records.py); sign_in is a plain function so it needs an explicit
  import. apply_import signature confirmed (session, *, tenant_id, import_id, candidates,
  roles, today).
  ENVIRONMENT — the implementer's "63 failures" were its own env, not the code.
  Suite cannot run off repo-root .env (points at Koyeb; conftest refuses remote).
  Wrote .superpowers/sdd/test-env.sh — SOURCE IT IN EVERY IMPLEMENTER DISPATCH.
  The role split is load-bearing: DATABASE_ADMIN_URL=postgres superuser (bypasses RLS),
  DATABASE_URL=expressautomate_app (obeys it). Both as superuser => 138 spurious RLS
  failures on correct code. alembic upgrade head must run first; it creates the role.
  VERIFIED GREEN BASELINE at aa70765: 1640 passed, 1 skipped.
  Migration head independently confirmed = 314cc3da9ced, as the plan states.
Task 1: complete (aa70765..1504b70, review clean — spec OK, 2 Minor unused imports).
  candidates.owner_id + composite FK with column-qualified SET NULL (owner_id) + backfill
  from created_by. 1642 passed, 1 skipped (= baseline 1640 + exactly the 2 new tests).
  NOTE: Task 0 had already put owner_id into conftest's make_candidate, against a column
  that did not exist — latently broken, never called, now valid.
Task 1 lint: 1504b70..1ad8979 (ruff --fix, 3 unused imports; 25 affected tests pass).
Task 2: complete (1ad8979..7189cb5, review clean — zero findings).
  candidate_shares model + migration c1a0d5e7b202 + registered in models/__init__.py.
  RLS block byte-for-byte identical to opportunity_shares bar the table name; model/migration
  parity checked name-for-name on every column, 2 CHECKs, 3 composite FKs, 5 indexes.
  1645 passed, 1 skipped (= 1642 + exactly 3).
Task 3: complete (7189cb5..c8ff87c, review approved). candidate_access_requests +
  migration c1a0d5e7b203 + registered. RLS byte-identical; model/migration parity checked
  incl. server_default 'pending'. 1646 passed, 1 skipped.
  MINOR CARRIED -> Task 11: the test proves two PENDING rows collide but NOT the
  decline-then-reask path, which is the entire reason the index is partial. Add it in
  Task 11 where decline exists and the path is end-to-end testable.
  Autogenerate drift ix_candidate_activities_candidate_created is genuinely pre-existing
  (from 20260729_1900_candidate_activities.py) — not ours, left alone.
Task 4: complete (c8ff87c..2ce2039 impl, ..75ab57d fix round 1; re-review approved).
  CandidateFieldOverride gains user_id. user_id IS NULL = permanent AGENCY-WIDE tier
  (import protection); non-NULL = one recruiter's judgement. NO backfill of legacy rows.
  Widened UNIQUE + a SECOND partial unique index WHERE user_id IS NULL (a NULL never
  collides in a UNIQUE, so the constraint alone does not bound the tenant-wide tier).
  FK is CASCADE not SET NULL, so a departed recruiter's opinion can't become agency-wide.
  *** BRIEF STEP 5 WAS WRONG and the implementer was right to refuse it: writing
  user_id=caller for EVERY patched field would privatise a corrected full_name and let
  the next import overwrite the name agency-wide. PATCH now ROUTES by the fact/judgement
  split in candidate_overrides.py. The NULL branch must infer on the PARTIAL INDEX
  (index_elements + index_where), NOT ON CONSTRAINT — a NULL never conflicts on the
  4-col constraint and naming it raises instead of updating. ***
  Brief's caller list was also wrong: imports/undo.py does NOT call overridden_fields
  (it counts rows for a warning, all tiers, correctly). Real sites: api/candidates.py:521
  (signed-in user), api/candidate_roles.py:216 and imports/apply.py:248 (both owner_id).
  current_title/current_employer classified SHARED because apply_derived writes them from
  shared role history; a per-user reading would drift. Closest call, revisit if it bites.
  1654 passed, 1 skipped.
Task 5: complete (75ab57d..f91c9ed, +lint 8e0f1b1; review approved, 2 Minor).
  visible_candidates / can_edit_candidate / load_visible_candidate (404) /
  load_editable_candidate (403) / candidate_shared_with_me_exists, added alongside the
  opportunity siblings in visibility.py. NO mailbox term — deliberate, candidates never
  come from the email pipeline. Correlated EXISTS verified sound against the proven
  opportunity pattern. Tenant boundary stays RLS, no explicit tenant_id term (same as
  opportunities). 1656 passed, 1 skipped.
  MINOR CARRIED -> Task 7: no test yet for the 404-vs-403 inversion (invisible => 404,
  visible-but-not-editable => 403). It is the subtlest rule in the feature. Cover it there.
Task 6: complete (bf10681..8278769, review approved, 3 Minor). FULL delivery path per the
  user's pre-flight decision: 6 kinds + CANDIDATE_EVENT_KINDS into ALL_EVENT_KINDS,
  candidate_events.py, subject_id protocol on BOTH event types (OpportunityEvent.subject_id
  returns opportunity_id — value unchanged, traced), emit_candidate_event, 6 _HEADLINE/
  _TEMPLATE_FOR entries + render branches, kind-prefix branch in jobs.py
  _send_claimed_delivery. E2E test drives a candidate.shared row to STATUS_SENT.
  1667 passed, 1 skipped (+11).
  *** SHIPPED BUG FOUND, confirmed by the reviewer, logged as SEPARATE work (chip
  task_0c8b791d): opportunity.shared and opportunity.assigned ARE emitted
  (opportunity_shares.py:164, opportunities.py:1104,1337) but are absent from _HEADLINE/
  _TEMPLATE_FOR -> KeyError in the delivery worker, which retries and stalls the queue. ***
  *** KOYEB ENV: new setting WHATSAPP_TEMPLATE_CANDIDATE_UPDATE is EMPTY. Per-service env
  vars are not in this repo (see CLAUDE.md). Must be set on api AND worker before candidate
  events go out over WhatsApp, or Meta rejects name:"" remotely with no local guard. ***
  MINOR CARRIED -> Task 11 / final: a share's `note` and `actor_name` are emit-time only;
  the worker rebuilds from the outbox row and renders them "Not mentioned". The note is
  REAL data merely unpersisted. Correct fix is persisting it, not changing the render.
  MINOR: test_a_deleted_candidate asserts "no send" but not that the row reached FAILED.
Task 7: complete (8278769..c3776d3 impl, ..6f788df fix 1; review approved).
  THE STRUCTURAL TEST EARNED ITS KEEP IMMEDIATELY: brief scoped it to candidates.py, but
  the test follows the MODEL not the filename and surfaced 25 unguarded by-id routes
  across 6 modules — candidates.py(10), candidate_roles(5), candidate_documents(3),
  candidates_avatar(3), candidate_whatsapp(2), sourcing(2), opportunities::get_eligibility.
  REAL PRE-EXISTING LEAKS FIXED: sourcing withdraw_submission had NO candidate load at all;
  get_eligibility + record_submission read Candidate under RLS alone; list_candidates'
  stage-count counted the WHOLE TENANT, leaking the size of a colleague's book.
  DEVIATION UPHELD: brief's single EXEMPT dict drops a route from BOTH assertions, so
  claim_candidate/log_activity would have silently lost READ coverage. Added
  EDIT_ONLY_EXEMPT filtering only the mutating test. (The reference file has the same
  latent flaw for start_sourcing.) AST machinery copied byte-for-byte, untouched.
  Only ONE pre-existing test adjusted (owner_id on its fixture), reviewer confirmed.
  1673 passed, 1 skipped, 2 xfailed.
  *** PROCESS CORRECTION (user, mid-turn): I took the sourcing guard question to the USER
  when it was a DESIGN call with a codebase precedent. It should have gone to FABLE first;
  only genuine product/business decisions go to the user. Applied for the rest of this run. ***
  Fable UPHELD the decision (withdraw=edit, record=visibility) — real axis is "whose record
  does it constrain", and a recipient's submission blocks only THAT ONE client, not the
  owner elsewhere. AMENDMENT FABLE FOUND: a recipient who mis-records now cannot self-undo.
  candidates.py is at 1489/1500 LOC — Task 12 must not add to it.
Task 7 fix 2 (6f788df..f388012, Fable's amendment): withdraw_submission now succeeds on
  EDIT rights OR own-row (the recorder can undo their own misclick), matching
  opportunity_shares' delete idiom. Uses can_edit_candidate (EDIT_CHECK), which the
  structural test treats as equivalent to EDIT_GUARD — no re-exemption needed.
  1675 passed, 1 skipped, 2 xfailed.
Task 8: complete (f388012..3eaa3fa impl, ..71f627a fix 1). Thin 409 on a colliding create.
  Helpers went to candidate_matching.py NOT candidates.py (which was 1489/1500; now 1494).
  *** PRIVACY HOLE CAUGHT IN REVIEW: abbreviate() only initialled the LAST token, so a
  full_name that IS an email ("weiming@example.com" — recruiters paste emails into name
  fields) was returned verbatim, and "John Tan 9123 4567" -> "John Tan 9123 4.". The
  payload SHAPE was safe by construction but its CONTENT was not; the original test only
  passed because its fixture kept the phone in another column. Rule now: <=3 leading
  tokens, initial the last, mask any token matching @|\d{4,} with a bullet, never empty. ***
  Also .one() -> .one_or_none() (a row merged between find_candidate and the holder read
  was a 500), and isouter -> inner join to make the invariant structural.
  1683 passed, 1 skipped, 2 xfailed.
  Fix 1 re-reviewed and APPROVED. Masking runs on ORIGINAL tokens before truncation and
  initialling, so a contact-like last token is replaced outright, never re-exposed.
  MINOR RESIDUAL for final review: a spaced NRIC-like name ("S12 34 56 A") leaks a leading
  fragment, since no single token hits \d{4,}. Truncation to 3 tokens caps the exposure.
Task 9: complete (71f627a..352903d, review approved). patch_collision() in
  candidate_matching.py (composes find_candidate + held_by_colleague, no duplication),
  3-line call site in update_candidate. Covers BOTH email and phone changes.
  PLAN PREMISE WAS WRONG, recorded for honesty: the plan claimed a colliding PATCH gave a
  500 leaking uq_candidates_tenant_email. It did not — a pre-existing IntegrityError catch
  already returned 409 with a plain STRING detail and never leaked the constraint name.
  The real improvement is disclosure SHAPE (thin payload shared with the create path), not
  a leak fix. 1685 passed, 1 skipped, 2 xfailed.
  *** IMPORTANT FOR TASK 12: app/api/candidates.py is now 1498/1500 LOC. Task 12 must
  modify merge_candidate in that file and has ~2 lines of headroom. An extraction has to
  come FIRST. Consult Fable on where to extract before dispatching Task 12. ***
  MINOR for final: the non-colliding PATCH test uses current_title, so the self-match arm
  (an unchanged email on an editable row) is never independently exercised.
Task 10: complete (352903d..8a68dbf, review approved, 1 Minor). claim/assign in the new
  candidate_ownership.py; scope= filter via candidate_scope() in visibility.py (kept
  candidates.py from breaching — it is now EXACTLY 1500/1500, THE NEXT LINE BREACHES).
  *** SAME LESSON AS THE JOB-ORDER CLAIM, RELEARNED: guard-first gives the race LOSER 404,
  not 409, because losing makes the row invisible to them. The atomic UPDATE must come
  FIRST, then rowcount==0 disambiguates absent(404) vs taken(409). Reviewer confirmed it
  matches opportunities.py:952-1022 statement for statement, and that it leaks nothing:
  every row the UPDATE can match is unowned, and unowned is the FIRST OR term of
  visible_candidates for every role. ***
  Brief invented a notification_outbox table; the real one is notification_deliveries
  (event_kind, tenant_id, subject_id, destination_id), and emit_candidate_event lives in
  notify/dispatch.py not candidate_events.py. Test seeds a verified destination + active
  subscription, else the assertion is vacuous.
  Neither xfail removed — both also need candidate_shares.py (Task 11). Correct.
  frontend/route-manifest.json regenerated (legitimate: two new routes).
  1692 passed, 1 skipped, 2 xfailed.
  MINOR for final: claim does not reject record_status == MERGED, unlike archive/unmerge —
  a merged tombstone with a NULL owner is claimable and returns 200.
Task 11: complete (8a68dbf..2507a07, review approved, 2 Minor). candidate_shares.py — share
  create/list/delete, request_candidate_access (never calls load_visible_candidate, by
  design and by exemption), owner-scoped inbox, grant/decline sharing one _resolve that
  writes share + resolution columns in ONE transaction. Router registered ABOVE
  candidates.router so /candidates/access-requests is not eaten as a UUID.
  BOTH xfail(strict=True) marks REMOVED — they went XPASS the moment the module existed,
  which is the mark doing its job. candidates.py gained ZERO lines (still 1500).
  Carried Task 3 finding CLOSED: decline-then-reask proven (new 200, different id, table
  ends ["declined","pending"]) — the whole reason the index is partial.
  Improvement over brief: recipient_user_ids=() not None for an unowned candidate, since
  None means "everyone".
  1698 passed, 1 skipped.
  MINOR carried: no happy-path test for the scope='tenant' INSERT, and none for the four
  delete-authorisation arms (esp. that a recipient cannot delete ANOTHER recipient's row).
FABLE EXTRACTION DECISION (candidates.py is at 1500/1500, Task 12 must modify merge):
  Extract merge_candidate + unmerge_candidate + MergeRequest + _lock_pair to a new
  app/api/candidate_merge.py, as ITS OWN COMMIT before Task 12. Frees ~185 net lines.
  _load and _serialize STAY in candidates.py (export_candidate and candidate_roles use
  them) and are imported FROM it — no circularity, candidates.py never imports the new
  module. main.py needs the router; route-manifest and the guard test need NO change
  (paths unchanged; _modules() globs api/*.py so the new file is auto-covered).
Task 11 follow-ups (2507a07..6efd408..09229c9):
  6efd408 — 5 tests: tenant-broadcast happy path + all four share-delete arms, incl. the
    load-bearing denial that a recipient cannot delete ANOTHER recipient's row. Denials
    proven by temporarily loosening the production check, then reverting. No prod bug.
  09229c9 — Fable's extraction, verbatim move: candidate_merge.py (222 LOC) takes
    merge_candidate, unmerge_candidate, MergeRequest, _lock_pair. _load/_serialize stayed
    in candidates.py and are imported FROM it. candidates.py 1500 -> 1302, so Task 12 has
    room. route-manifest regenerated with ZERO diff and the guard test needed no change,
    both exactly as Fable predicted.
  1703 passed, 1 skipped.
Task 12: complete (09229c9..15e1bc0 impl, ..1ca270e fix 1; review approved).
  merge_candidate now loads BOTH sides through load_editable_candidate and operates on the
  GUARDED result (previously a recruiter could merge INTO a colleague's candidate,
  destroying one record and enriching one they were never shown). unmerge deliberately does
  NOT write owner_id — it survives the merge, so reviving restores the original owner by
  doing nothing. Reviewer confirmed the extraction commit was a genuinely PURE move and
  that _lock_pair still sorts by uuid.bytes BEFORE the guard loads, so A->B and B->A queue
  rather than deadlock.
  *** MY PLAN'S TEST WAS WRONG: test_unmerge_restores_the_original_owner seeded the merged
  row with owner_id=me, so the presser WAS the original owner and owner_id=user_uuid would
  still have satisfied it. It passed whether or not the bug existed — worse than no test,
  because it read as coverage. Now the row is owned by a colleague and the caller uses
  role='owner'. Proven by injecting owner_id=user_uuid: test failed, reverted, passed. ***
  Sibling merge tests checked for the same shape of weakness — none found.
  1705 passed, 1 skipped.
Task 13: complete (1ca270e..d5c64db impl, ..6a18dbc fix 1; review approved).
  _import_tenant -> _import_owner returns (tenant_id, uploaded_by); owner_id=uploaded_by on
  create; update branch skips + counts when candidate.owner_id not in (None, uploaded_by).
  Matching stays TENANT-WIDE (a visibility-filtered lookup would miss an invisible row then
  die on the unique index at flush). SCOPE RULING: the new candidate_imports.held_by_colleagues
  COLUMN (migration c1a0d5e7b205) is NECESSARY, not creep — the import UI never sees
  ImportOutcome; every counter is read back off the row via serialize() in candidate_imports.py.
  *** HOLE THE GUARD MISSED, found in review: a skipped candidate never entered `seen`, so
  _candidate_for fell back to tenant-wide find_candidate and _apply_roles wrote a
  CandidateRole onto the HELD candidate — the same bulk edit by a different path. Fixed by
  recording skipped ids in a `held` set at the single ownership test, passed to the roles
  path, rather than duplicating the rule. Not folded into held_by_colleagues (that counter
  means candidates, and inflating it would make the number lie) — a RowProblem instead. ***
  MINOR pinned: uploaded_by NULL degrades conservatively (skips every owned row, gains no
  permission) but creates land unowned. Now tested and commented rather than accidental.
  1709 passed, 1 skipped.
=== BACKEND COMPLETE (Tasks 0-13). Remaining: 14 frontend, 15 verification. ===
Task 14: complete (6a18dbc..ef23e7f UI, ..2c6051f backend gaps, ..159ba13 wiring).
  The frontend found TWO REAL BACKEND GAPS the backend reviews could not:
  (1) the 409 carried NO candidate id, but POST /candidates/{id}/access-requests is keyed
      by id and the 409 is the only way B learns the row exists — so can_request_access
      was unactionable and the button rendered disabled.
      *** FABLE RULED MY SPEC LINE "not even the candidate id" WAS SECURITY THEATRE: the
      409 already discloses the maximal facts (existence + holder); a UUID is unguessable,
      tenant-scoped, and every route behind it is guarded. Struck. ***
  (2) _serialize never emitted owner_id or a can-edit flag, so the panel could not show the
      owner or DISABLE the edit control. Now emits owner {id,name}|null + can_edit — the
      server's rule PUBLISHED, so the UI never re-derives owner==me||role==owner.
      Owner name joined ONCE (LEFT OUTER on row queries only, not the count/aggregates).
  Also fixed en route: readError returned FastAPI's detail even when it was an OBJECT,
  handing React an object to render — a colliding create was likely CRASHING the dialog.
Task 15 verification: all 5 migrations round-trip downgrade 314cc3da9ced -> upgrade head.
  Largest touched file 1356/1500. Zero xfail left in the guard test.
FINAL WHOLE-BRANCH REVIEW (Fable) — READY WITH FIXES. Two cross-module gaps that no
  per-task review could have caught, both now closed:
  (a) b6ecd07 — MERGE STRANDED SHARES. candidate_shares and pending access requests were
      never moved from loser to target, so a colleague granted access lost it silently the
      moment someone merged the person — the last step of the story the feature exists for.
      Semantics: a share on either row is a share on the survivor; move what does not
      collide, drop what does (three partial indexes make a naive UPDATE raise). Resolved
      requests STAY on the loser as history. Unmerge does NOT give shares back — the grant
      was sight of a PERSON, and the survivor kept the data.
  (b) 6aa93b8 — SOURCING LEAKED PRIVATE CANDIDATES, and it was the structural guard's exact
      blind spot: the guard covers by-id reads, sourcing reaches candidates by SET
      MEMBERSHIP and returned explanation, reasons and CV evidence QUOTES for colleagues'
      private rows — far more than the 409 ever discloses.
      *** FABLE'S RULING, and it is the good third option: keep scoring agency-wide (an
      agency that cannot shortlist its own book has no reason to run sourcing) and REDACT
      AT READ to exactly the 409 tier. Not a new tier — the existing one on one more
      surface, mirroring the import rule "match tenant-wide, disclose at the edge". Score
      kept: it reveals fit, not content. masked_candidate() extracted so the 409 path and
      sourcing share ONE masking implementation and cannot drift. ***
      Guard test extended with a sweep that fails on select(Candidate) in any app/ module
      outside app/api/, with a written exemption per legitimate case. Verified it bites.
  (c) 610f8ee — the redacted match rendered a RAW UUID in the UI (namesFor fetched by id
      and 404'd). Now renders the masked name + "held by X" + request access.
=== ALL 16 TASKS COMPLETE. Backend 1730 passed 1 skipped; frontend 221 passed / 24 files;
    ruff clean; tsc clean; migrations round-trip. NOT MERGED. ===
FABLE RE-REVIEW of the four post-review commits (159ba13..01fc89d): READY TO MERGE.
  Verified adversarially: masked_candidate() is genuinely ONE implementation shared by the
  409 path and sourcing (grep-confirmed both call sites, no third); the redacted payload
  carries no evidence id/timestamp/explanation; BOTH stored-run read paths (latest_sourcing
  and one_sourcing_run) route through _with_matches and read_matches/serialize_match have
  no other caller; the collide-drop DELETE requires an equivalent grant to ALREADY exist on
  the target (EXISTS on the exact partial-index key, IS NOT DISTINCT FROM for the broadcast
  NULL) so it can never drop the only row granting access; all moves are inside the single
  merge transaction; the sweep's exemptions each bound disclosure at a named edge and
  workers are within its reach.
  Two Minor findings. FIXED: sourcing offered can_request_access=true even when the row had
  vanished between reads, sending the recruiter to a button that could only 404 — now
  gated on masked is not None (26 sourcing tests pass, ruff clean).
  ACCEPTED: a pending request moved to the target can coexist with an existing share for
  the same requester; granting is idempotent and changes no access.
=== BRANCH COMPLETE AND FABLE-APPROVED. NOT MERGED. ===
