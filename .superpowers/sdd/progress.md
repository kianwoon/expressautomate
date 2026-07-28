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

ENVIRONMENT BROKEN — 2026-07-29. I recreated the ea-test-db container and could not restore
  a clean full-suite run. Best result 866 passed / 10 failed; repeat runs vary (10/23/27
  failures), all InvalidPasswordError for expressautomate_app on connections opened later in
  the run. Cause: the RLS migration (20260726_1800) ALTERs the app role's password to
  settings.DATABASE_APP_PASSWORD, which comes from the production .env symlink, so anything
  re-running it mid-suite invalidates fresh connections. Failures are confined to concurrency
  and worker-sweep tests, none of which piece 2 touches. NOT verified as green locally.
  Next step: provision the DB the way CI does (see .github/workflows), or push and let CI judge.
