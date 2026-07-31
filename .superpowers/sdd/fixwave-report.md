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

## Second pass: the two reopened findings

**1. The IntegrityError guard was not narrow.** `create_opportunity` wrapped its
flush in `_client_link_conflict_becomes_422()` unconditionally, so any
constraint failure on a manual create — including one on a request carrying no
`client_id` at all — came back as "That client is not in this agency." The
flush is now inside `if body.client_id is not None:`, with a plain flush
otherwise, and the context manager's docstring says outright that it cannot
tell one constraint from another and so is the caller's to enter only when a
client id was supplied.

Pre-fix failure (`tests/test_opportunity_claim.py::test_an_unrelated_violation_is_not_blamed_on_the_client`):

    assert created.status_code == 500
    E   AssertionError: 422
    E   assert 422 == 500

The violation used is `ck_opportunities_salary_period_known`. The route never
writes `salary_period`, so the test puts an out-of-vocabulary value on the row
with a `before_insert` listener; what is under test is that *any* non-client
`IntegrityError` off that flush escapes as a 500 rather than being blamed on a
client the request never named.

**2. The placement resync fixed the display, not the harm.** A colleague's
change arriving while the form was dirty was rendered nowhere, the baseline
advanced silently, and Save overwrote a regulatory judgement its author never
saw. The recruiter's in-progress edit is still kept — but now they are told.
`PlacementForm` raises a notice naming the incoming value ("Someone else set
the placement type to S Pass while you were editing. Your own choice is still
below, and Save will send it.") with *Use theirs* and *Keep mine*. Keep mine
dismisses and Save sends their own value; Use theirs loads the colleague's
values into the selects, after which there is nothing left to write and Save is
disabled.

Pre-fix failure (`app/dashboard/job-order-placement.test.tsx`):

    × names the new value, keeps the recruiter's own choice, and sends theirs on Keep mine
      → Unable to find role="status"
    × takes the colleague's value on Use theirs, and then has nothing to send
      → Unable to find role="status"

Verified: backend 1640 passed, `ruff check` clean; frontend 195 tests across 22
files, `npm run build` succeeded. (The previous report's "23 test files" was
wrong; it is 22.)
