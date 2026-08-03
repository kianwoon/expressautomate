# Mailbox intake pause — design

**Date:** 2026-08-03
**Status:** approved, not yet implemented

## Problem

A recruiter goes on vacation. While they are away their mailbox keeps feeding
the ingestion pipeline, so email they will not read for two weeks is classified,
extracted, and turned into job orders. They want a switch that stops that, and
they want it in the dashboard header where the `Live` pill sits.

## What this is not

**`Live` is not the switch.** [`LiveLight`](../../../frontend/app/dashboard/live-light.tsx)
renders `Live` / `Connecting` / `Not updating` from `useLiveStatus()` — it is a
read-only report of whether the SSE stream is delivering. Making it clickable
would give one word two unrelated meanings, and a paused mailbox would read as
"this page is broken" rather than "my intake is off". The pause gets its own
control, sitting next to it.

**This does not stop job orders being assigned to the away recruiter.**
Assignment is client-driven: a new opportunity inherits the matched client's
`assigned_user_id`
([`persist.py:302`](../../../backend/app/services/ingest/persist.py),
[`client_matching.py:208`](../../../backend/app/services/client_matching.py)).
Mail from the recruiter's client arriving in a *colleague's* mailbox still
creates a job order assigned to the vacationing recruiter. That was raised and
the scope was confirmed as mailbox-level anyway. Anything that redistributes
work during an absence — cover/delegate, queue-drop, "owner away" flags — is a
separate design.

## Decisions

| Question | Decision |
|---|---|
| What does "suspended" stop? | This recruiter's **own mailbox** feeding the pipeline. |
| Mail arriving during the pause? | **Never ingested.** No catch-up, no backfill. |
| What does resume do? | Ingestion restarts **from the current date and time**. |
| Scope | Per mailbox, set by its owner. |

The "no catch-up" decision is what keeps this simple: resume is a clean restart,
not a replay, so there is no queue of stale work waiting on return.

## Data model

One new nullable column on `mailboxes`:

```
ingest_paused_at  timestamptz  NULL
```

`NULL` means intake is running. A timestamp means it is paused, and records
since when — the UI shows that, because the failure mode of this feature is
forgetting it is off.

### Why not reuse `mailboxes.status`

`status` is auth state — `'active'` or `'needs_reauth'`
([`jobs.py:137`](../../../backend/app/workers/jobs.py)). Adding a `'paused'`
value looks like the smaller change but is wrong twice:

1. `status = 'active'` gates more than polling. `subscriptions_due_for_renewal()`
   joins on it too
   ([`20260727_0841_operator_resolvers.py:79`](../../../backend/migrations/versions/20260727_0841_operator_resolvers.py)).
   A mailbox parked at `'paused'` would stop renewing its Graph subscription and
   the subscription would lapse mid-vacation.
2. Pause and re-auth are independent facts. A token that expires while the
   recruiter is away must still be recorded as `needs_reauth`; a single enum
   column can only hold one of the two.

A separate column keeps subscription renewal alive during the pause and lets
both facts be true at once.

## Backend

**Gate.** `active_mailboxes()`
([`20260727_0841_operator_resolvers.py:126`](../../../backend/migrations/versions/20260727_0841_operator_resolvers.py))
gains `AND m.ingest_paused_at IS NULL`. It is the resolver `delta_sync_all()`
fans out over ([`tasks.py:409`](../../../backend/app/workers/tasks.py)), so the
gate lands in one place and the worker needs no change. Redefining the function
requires a new Alembic migration alongside the column.

`subscriptions_due_for_renewal()` is deliberately **left alone** — a paused
mailbox keeps its Graph subscription current so resume does not need to
recreate it.

**Resume starts from now.** On resume, clear `delta_link` and set
`initial_sync_from = now()`. That pair is the existing "start here" mechanism
([`mailbox.py:49-50`](../../../backend/app/models/mailbox.py)), already used by
the lookback-extension endpoint
([`api/mailbox.py:51-54`](../../../backend/app/api/mailbox.py)).

> **Implementation must verify, not assume:** that `initial_sync_from` actually
> bounds the Graph query when `delta_link` is `NULL`
> ([`services/graph/delta.py`](../../../backend/app/services/graph/delta.py)).
> If a null delta link triggers an unbounded initial sync, resume would replay
> the entire vacation window — the exact opposite of the decision above. This is
> the one load-bearing unknown in the design and needs a test that pins it.

`backfill_completed_at` must be handled so that resume does not kick off a
backfill of the paused window.

## API

Two routes beside the existing ones in
[`backend/app/api/mailbox.py`](../../../backend/app/api/mailbox.py):

```
POST /api/mailbox/pause    -> { paused_at }
POST /api/mailbox/resume   -> { resumed_from }
```

Both act on the **caller's own** mailbox, resolved from the session the same way
`GET /api/mailbox/settings` does. No mailbox id in the path, so there is no
cross-user surface to guard wrong. `GET /api/mailbox/settings` gains
`ingest_paused_at` so the UI can render state on load.

Both are idempotent: pausing a paused mailbox is a no-op, and resuming a running
one does not reset the cursor.

## Frontend

A pill next to `LiveLight` in
[`job-orders.tsx:216`](../../../frontend/app/dashboard/job-orders.tsx),
rendered by a new `IntakePause` component in its own file:

- **Running:** `Intake on`.
- **Paused:** `Intake paused` plus since-when, styled to stay conspicuous rather
  than receding into the header. Never colour alone — the state is in words, as
  `LiveLight` already establishes.
- Clicking toggles; resuming warns once, in plain language, that mail received
  during the pause will not be picked up.

Mirrored in `/settings` beside the lookback control
([`lookback.tsx`](../../../frontend/app/settings/lookback.tsx)) — the dashboard
pill is the fast path, settings is where someone goes looking for it.

## Testing

- Migration applies and reverts; the redefined `active_mailboxes()` excludes a
  paused mailbox and still returns active ones.
- `subscriptions_due_for_renewal()` still returns a paused mailbox.
- `delta_sync_all()` fans out to no paused mailbox.
- Pause then resume: `delta_link` cleared, `initial_sync_from` at resume time,
  no backfill queued.
- **The pinning test:** a mailbox paused with mail arriving in the window, then
  resumed, ingests none of that mail. This is the one that catches the unbounded
  initial-sync risk flagged above.
- Both routes idempotent; a user cannot pause another user's mailbox.
- Frontend: pill renders both states, toggle calls the route, paused state
  survives reload.

## Out of scope

- Scheduled or date-ranged absence, and auto-resume on a return date. A manual
  toggle someone forgets is a real failure mode; it is deferred, not dismissed,
  and the `ingest_paused_at` timestamp is what a later reminder would build on.
- Redistributing an away recruiter's client-driven job orders.
- Suppressing notifications for a paused user.
- Any tenant-wide or admin-set pause.
