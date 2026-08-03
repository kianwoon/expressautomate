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
([`persist.py:320`](../../../backend/app/services/ingest/persist.py),
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
   ([`20260727_0841_operator_resolvers.py:79`](../../../backend/alembic/versions/20260727_0841_operator_resolvers.py)).
   A mailbox parked at `'paused'` would stop renewing its Graph subscription and
   the subscription would lapse mid-vacation.
2. Pause and re-auth are independent facts. A token that expires while the
   recruiter is away must still be recorded as `needs_reauth`; a single enum
   column can only hold one of the two.

A separate column keeps subscription renewal alive during the pause and lets
both facts be true at once.

### Migration note

Redefining `active_mailboxes()` with `CREATE OR REPLACE` must repeat
`SECURITY DEFINER` and its `SET search_path`. Omitting them silently reverts the
function to `SECURITY INVOKER`, and the app role runs under
`FORCE ROW LEVEL SECURITY` — the resolver would then see only one tenant's
mailboxes and the sweep would quietly stop working for everyone else. The
downgrade must restore the previous body verbatim.

## Backend

### There is no single gate — mail enters by three doors

The obvious change is to add `AND m.ingest_paused_at IS NULL` to
`active_mailboxes()`
([`20260727_0841_operator_resolvers.py:126`](../../../backend/alembic/versions/20260727_0841_operator_resolvers.py)),
the resolver `delta_sync_all()` fans out over
([`tasks.py:409`](../../../backend/app/workers/tasks.py)). That is necessary and
**not sufficient**, and believing otherwise is the way this feature ships
broken. The scheduled delta sweep is the *slow* path. The primary intake path is
the webhook: a Graph notification arrives at
[`graph_webhook.py:175-192`](../../../backend/app/api/graph_webhook.py), records
the notification and enqueues `fetch_email` directly — it never consults
`active_mailboxes()`. Since this design deliberately keeps the subscription
alive during the pause, every vacation email would be ingested in real time
through a gate that was never there. `flush_notifications` and the
lifecycle-triggered `delta_sync_mailbox`
([`graph_webhook.py:43`](../../../backend/app/api/graph_webhook.py)) are the
third door.

**Gate at the worker job entry points, which every door funnels through.**
`fetch_email` and `delta_sync_mailbox` ([`jobs.py`](../../../backend/app/workers/jobs.py))
already load the mailbox row; each returns early when `ingest_paused_at` is not
null. That is the authoritative check and it cannot be routed around.

Two supporting changes, neither of which is load-bearing on its own:

- `active_mailboxes()` gains the predicate, so the sweep does not queue work
  that will immediately be discarded.
- The webhook drops a paused mailbox's notification early, before
  `record_notification`, so the pause does not accumulate rows that nothing will
  ever fetch.

`subscriptions_due_for_renewal()`
([`20260727_0841_operator_resolvers.py:79`](../../../backend/alembic/versions/20260727_0841_operator_resolvers.py))
is deliberately **left alone** — a paused mailbox keeps its Graph subscription
current, so resume does not have to recreate it.

### Resume starts from now — and the obvious way does not work

Clearing `delta_link` does **not** mean "start from now". `delta_sync_mailbox`
([`jobs.py:1007`](../../../backend/app/workers/jobs.py)) calls `sync_mailbox`
with no `since`, and `_walk_start` in
[`delta.py`](../../../backend/app/services/graph/delta.py) with a null
`delta_link` and no `since` returns the bare folder delta URL — an uncapped walk
of the entire folder history. A resume built that way would replay the whole
vacation window, which is precisely the decision it is meant to honour.

`initial_sync_from` does not save it either: that field bounds
`backfill_mailbox_job` ([`jobs.py:951`](../../../backend/app/workers/jobs.py)),
a different path. The lookback endpoint
([`api/mailbox.py:52`](../../../backend/app/api/mailbox.py)) clears
`backfill_completed_at` and enqueues a backfill — it never touches `delta_link`,
so it is not the "existing pair" an earlier draft of this document claimed.

**Resume must establish a fresh cursor:** run a `since = now()` filtered walk to
obtain a new `deltaLink`, and store that. Ingestion then continues from a cursor
that has the paused window already behind it.

`backfill_completed_at` is **left set** on resume. Clearing it would queue a
backfill of exactly the window this feature exists to skip.

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
  paused mailbox, still returns active ones, and is still `SECURITY DEFINER`
  (assert on `pg_proc.prosecdef`, or the RLS failure above ships silently).
- `subscriptions_due_for_renewal()` still returns a paused mailbox.
- `delta_sync_all()` fans out to no paused mailbox.
- **A webhook notification for a paused mailbox ingests nothing.** This is the
  test that catches the real intake path; without it the feature passes its
  suite and fails in production.
- `fetch_email` and `delta_sync_mailbox` each return early when the mailbox is
  paused, even when invoked directly — the gate is at the job, not the caller.
- Pause then resume: a new `delta_link` is stored, `backfill_completed_at` is
  left set, no backfill is queued.
- **The pinning test:** a mailbox paused with mail arriving in the window, then
  resumed, ingests none of that mail. This is the one that catches an unbounded
  replay if the fresh-cursor logic regresses.
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
