# Fixwave — three review findings

Commits: `142ddbd` (finding 1), `6ef8edb` (finding 2), `1ddfd02` (finding 3).

## 1. An unchecked client id gave a 500 (backend)

`create_opportunity` now pre-checks `body.client_id` with the same RLS-scoped
SELECT `set_opportunity_client` uses, and refuses with the same sentence —
"That client is not in this agency." The lookup is now one shared helper,
`_load_client_in_agency`.

**The race:** a pre-check and the write after it are two statements, and no
pre-check closes the gap. Chosen answer: catch `IntegrityError` around the
write and convert it to the same 422, because the caller needs the same answer
whether the client vanished before their request or during it. The guard is
wrapped only around writes made because a `client_id` was supplied, so a
violation of any other constraint on the same row still surfaces loudly. The
manual insert is flushed inside the guard rather than left to the commit at the
end of the block, so the violation is caught where it can still be answered.

Tests (`tests/test_opportunity_claim.py`): nonexistent id → 422; another
agency's client → 422 (RLS makes it not found); and the race, made reproducible
by neutering the pre-check so what remains is exactly the state where the
delete landed after the check. All three fail against the pre-fix code.

## 2. The placement panel could overwrite an unseen change (frontend)

Both the panel's copy and the form's own selects now resync from the row.

- Synced on the placement **values** changing, not on the row object, which is
  new on every poll. The last values seen are held in a ref rather than
  compared against local state, so a save — which leaves the panel fresher than
  the list for a poll or two — does not flip back to the stale row and forward
  again.
- **Unsaved edits are kept.** If the recruiter has changed a select and a
  colleague's change lands, their choice stays on screen; replacing what
  someone is in the middle of choosing loses a decision just as surely as not
  showing them the change. The baseline moves, so Save still sends their
  choice, and the server holds the last write either way.

`app/dashboard/detail-panel-placement.test.tsx` covers both.

## 3. The contract test missed inline API paths (frontend)

`app/api.contract.test.ts` now also scans non-test sources under `app/` for
string and template literals starting with `/api/` and matches each against
the manifest. False positives are handled by construction, not a list:
comments are stripped (the only place an example path can live), and `${…}`
is substituted with the same placeholder the helpers get.

Stated plainly rather than skipped: a path assembled from pieces that are not
one literal cannot be seen without following the value.

No existing code writes an inline path, so `it.each` over an empty list would
assert nothing — the extractor itself is unit-tested on a sample instead.

**Mutation proof:** adding `void fetch("/api/definitely-not-a-route")` to
`detail-panel.tsx` failed the test with "/api/definitely-not-a-route is not a
route the API serves"; after removal `git status` showed only the contract
test modified.

## Verification

- backend `uv run pytest -q` — 1639 passed (baseline 1636); `uv run ruff check .` clean.
- frontend `npm test` — 193 passed / 23 files (baseline 190 / 21); `npm run build` passed.
