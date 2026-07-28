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
