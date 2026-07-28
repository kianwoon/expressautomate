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
