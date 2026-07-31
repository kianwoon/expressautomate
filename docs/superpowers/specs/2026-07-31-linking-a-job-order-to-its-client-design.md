# Linking a job order to its client

Decided 2026-07-31. Closes a gap in
[the assignment and sharing design](2026-07-30-job-order-assignment-and-sharing-design.md)
and [its screens](2026-07-31-job-order-sharing-ui-design.md), both shipped.

## The gap, and how it was found

An agency owner set every client's recruiter to themselves and the job orders
list still showed almost all of them unowned. Three rows out of eleven picked up
an owner; eight did not.

Nothing was broken. A client's recruiter reaches a job order by exactly two
paths, and both require the job order to carry a `client_id`:

1. **At ingestion** — a new email from that client produces a job order already
   assigned to the client's recruiter.
2. **On reassignment** — `PUT /api/clients/{id}/assignee` moves job orders
   `WHERE client_id = <that client>`.

Eight of the eleven job orders have no `client_id`, so neither path can reach
them:

| Job orders | Why `client_id` is NULL |
|---|---|
| 5 | the source email's `client_mentions` named **six distinct clients** each |
| 3 | the source email named **no** client at all |

The five came from a deliberate decision during the production migration on
2026-07-31. The original backfill was `UPDATE … FROM client_mentions`, which
picks an arbitrary row when an email refers to several companies — and
`client_id` drives who the work is routed to, so a confidently-wrong client
sends a job order to the wrong recruiter. The backfill was narrowed to fill only
where exactly one candidate existed.

That was the right call and this spec does not revisit it. **The defect is that
no way was ever provided to resolve the ambiguity afterwards.** `client_id` is
accepted only on create; there is no endpoint that sets it on an existing job
order, so those eight rows are permanently unlinkable through the product.

**A second, smaller thing made this hard to see.** The five ambiguous rows still
display a company name — "AvePoint", "Coda", "HTX" — because that comes from
`company_name_raw`, the AI extraction, not from the client link. They look
linked and are not.

## What this adds

### `POST /api/opportunities/{id}/client`

Body: `{client_id: uuid | null, adopt_client_recruiter: bool}`.

`null` unlinks — a mis-linked job order must be correctable, and the column is
nullable because a job order taken over the phone from an unrecorded company
legitimately has no client.

**Permission: editable, or unassigned.**

This is the subtle part and it is deliberate. `can_edit` refuses unassigned job
orders, because claiming is what makes a job order editable — that rule exists
so nobody edits work nobody has taken responsibility for. But every row this
feature exists to fix is unassigned, so gating on `can_edit` alone would make
the endpoint unable to solve its own problem.

Recording which company a job order came from is a factual correction, not an
act of ownership, and the queue is shared work by design. So:

```
may_set_client = assigned_user_id IS NULL
              OR assigned_user_id = :caller
              OR role = 'owner'
```

Someone else's *assigned* job order stays closed: 403, with the same wording the
other write routes use. An invisible one is 404, never 403.

### Adopting the client's recruiter

When the job order is **unassigned** and the client **has** a recruiter,
`adopt_client_recruiter` (defaulting to `true`) assigns it to them.

This mirrors the ingestion rule exactly — a job order goes to the client's
recruiter, not to whoever received the mail — so linking a client produces the
same outcome the pipeline would have produced had the link been there from the
start. Without it, fixing these eight rows is link-then-claim, twice the work
for the same result.

It stays a flag rather than an automatic consequence because linking a client
and taking ownership are two different intentions, and the response reports who
it went to rather than changing ownership silently:

```json
{"id": "...", "client_id": "...", "assigned_user_id": "...", "assignee_name": "Wei Kian"}
```

**An already-assigned job order never changes hands.** Linking a client is not a
way to take someone else's work — that is what the assign route is for, and it
has its own permission rule.

### Saying when a job order is unlinked

The detail panel gains a line stating that a job order is not linked to a
client, shown when `client_id` is NULL. It is the presence of an extracted
company name directly above that makes the blank field misleading, so the panel
says the true state in words rather than relying on an absence to convey it.

## Frontend

The client field on the detail panel reuses the type-to-search built for manual
creation in `frontend/app/dashboard/job-order-form.tsx` — extracted into
`client-search.tsx` so there is one implementation, not two. It searches
`GET /api/clients?q=`, which already exists; no new read is required.

Clients are paginated and an agency accumulates hundreds, which is why this is a
search rather than a dropdown — the same reason the manual-creation form does
it this way, and the reason it is not the members picker, which preloads because
an agency has 3–50 recruiters.

## Testing

- Linking a client to an unassigned job order adopts the client's recruiter, and
  the response names them.
- With `adopt_client_recruiter: false`, the job order stays in the queue.
- Linking a client that has no recruiter leaves the job order unassigned.
- An **already-assigned** job order does not change hands, whatever the flag says.
- The assignee may set the client on their own job order.
- A bystander gets 403 on someone else's assigned job order, and no client is
  written — a permission check placed after the update would leave the damage
  done.
- An invisible job order is 404, not 403.
- `null` unlinks, and does not change the assignee.
- The panel says a job order is unlinked when `client_id` is NULL, including
  when a company name is present.

## Not in this round

Bulk linking, and any attempt to re-resolve the five ambiguous rows
automatically. The evidence names six candidates each; a person picking from a
list is the only honest way to settle that, which is what this builds.
