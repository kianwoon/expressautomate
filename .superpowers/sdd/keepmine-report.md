# Keep mine merge, and the guard that stopped guessing

## Finding 1 — "Keep mine" reverted a colleague's untouched field

`frontend/app/dashboard/job-order-placement.tsx`. Added a `touched` ref with two
flags, not three: `placement`, and `requirement` covering the sex requirement
*and* its reason. **Keep mine** now merges the incoming row into every unit the
recruiter did not edit, so Save writes only what they decided.

**Why the pair is one unit.** The backend's check constraint refuses a
requirement without a reason. Merging one half while keeping the other could
post a pair that cannot exist — a female requirement carrying a colleague's
reason written for a male one, or a requirement with no reason at all. Editing
either half therefore marks the pair touched. That is the safe direction: the
recruiter keeps what they typed rather than having it silently swapped, and the
pair still travels together on the wire as it always did.

`touched` resets on the read-back after a save, on **Use theirs**, and on the
effect's fully-untouched resync path.

Pre-fix failures (2 new tests):

    × keeps only what the recruiter edited, and lets the colleague's other field stand
      → expected '' to be 'female'
    × keeps the requirement pair the recruiter edited and takes the colleague's placement type
      → expected '' to be 's_pass'

The genuine same-field conflict case ("Keep mine on a placement type both
touched sends the recruiter's") was already covered verbatim by the existing
test at the head of that describe block, and still passes. Added a new test that
**Use theirs** loads all three fields and leaves Save disabled.

## Finding 2 — the guard blamed the client for other constraints

`backend/app/api/opportunities.py`. `_client_link_conflict_becomes_422` now
inspects the failure and re-raises anything that is not the client foreign key.
The constraint name is read off `Opportunity.__table__` at import
(`_client_fk_constraint_name`) rather than spelled out, so a rename in the model
cannot leave the match silently dead. `_constraint_of` walks `exc.orig` and its
`__cause__` chain for asyncpg's `constraint_name`; an unattributable failure
surfaces as itself.

Docstring corrected: it no longer claims the link route is reached only with a
`client_id` (the unlink path enters the guard too), and no longer apologises for
being unable to tell constraints apart.

Pre-fix failure:

    tests/test_opportunity_claim.py::test_a_real_client_does_not_absorb_an_unrelated_violation
    AssertionError: 422
    assert 422 == 500

## Verification

- backend `uv run pytest -q` — 1641 passed (baseline 1640)
- backend `uv run ruff check .` — All checks passed
- frontend `npm test` — 198 passed across 22 files (baseline 195)
- frontend `npm run build` — succeeded
