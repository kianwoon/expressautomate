# Job order assignment and sharing

Decided 2026-07-30. Amends the opportunity model (§16, §17), the client model,
and the notification catalogue in
[the notification design](2026-07-28-notification-system-design.md).

Two facts about how a recruitment agency actually works were missing from the
model. First, **a client belongs to a recruiter** — one person takes care of
that account, and a job order from that client is that person's work, not the
agency's collective inbox. Second, **a job order travels**. A recruiter who
cannot fill a vacancy forwards it to a colleague who can, or throws it open to
the whole office. Today the system has neither: every opportunity is visible to
every user in the tenant, ownership is implicit in whose mailbox the mail
happened to land in, and an opportunity cannot exist without an email at all.

## Decisions

| Question | Answer | Why |
|---|---|---|
| What sharing creates | Visibility on the same row | One canonical opportunity. A forked copy makes the same vacancy exist twice, and every count, dedup and "who filled it" answer becomes ambiguous. |
| Default visibility | The assigned recruiter only | Private by default is what makes sharing a deliberate act rather than a notification. |
| Share access level | Read only | Sharing grants sight. Exactly one person can edit a job order, so there is no permission lattice to reason about. |
| Unassigned job orders | Visible to everyone, claimable | Unassigned work should be conspicuous, not hidden. A queue nobody can see is a queue nobody clears. |
| Which recruiter a job order goes to | The client's assignee | The mailbox was the transport. The client assignment is the authority. |
| Mailbox owner | Retains implicit read | The recipient has the original email in Outlook. Hiding the extracted version of a mail they can already read reads as a bug. |
| Client assignees | One primary, plus collaborators | "Who is responsible" needs exactly one answer for notification routing; cover during leave needs more than one name. |
| Elevated access | `role = 'owner'` sees everything | A three-person agency needs the boss to see the pipeline. |
| Share to all | One row, `scope='tenant'` | A recruiter hired next month inherits it. N per-user rows freeze the recipient list at the moment of sharing. |
| Manual job orders | Yes, `email_message_id` nullable | Job orders arrive by phone and WhatsApp. |
| Where visibility is enforced | Application query layer, not RLS | Argued below. |

Deferred: per-client default share rules, share expiry, team/group scopes,
transfer-of-ownership approval flows.

## Data model

### Prerequisite

`users` gains `UniqueConstraint(tenant_id, id)`. Every user foreign key added
below is composite — `(tenant_id, user_id)` referencing `(users.tenant_id,
users.id)` — the same idiom `client_mentions` and `client_contacts` already use
([client.py:100](../../../backend/app/models/client.py)). A plain `users.id`
reference would let a share row grant access to a user in another agency, which
is the one thing the whole tenant model exists to prevent. The constraint does
not exist yet; without it the composite FKs cannot be declared.

### Clients

| Column | Type | Notes |
|---|---|---|
| `assigned_user_id` | composite FK, `ON DELETE SET NULL`, nullable | The primary recruiter. Nullable because pipeline-proposed clients arrive unassigned, and because a departing recruiter's clients must not vanish with the account. |

**`client_collaborators`** — a new tenant-scoped table, unique on
`(tenant_id, client_id, user_id)`, with composite FKs to both parents. Cover and
backup recruiters. No primary flag: the primary lives on the client row, so
there is one place to read it and no way for the two to disagree.

Collaborators deliberately **do not** grant visibility of the client's job
orders. They are a record of who else knows the account. Making them an implicit
share would add a second, invisible path into the visibility predicate, and then
"why can Raj see this?" would have two possible answers. Cover that needs sight
of the work is an explicit share or a reassignment.

### Opportunities

| Column | Change | Notes |
|---|---|---|
| `client_id` | **new** — composite FK, `ON DELETE SET NULL`, nullable | There is no client link on the opportunity today. Without it, assignment is a copied user id with no record of which client drove it: reassigning a client cannot find its job orders, and a manual job order has no client at all. |
| `assigned_user_id` | **new** — composite FK, `ON DELETE SET NULL`, nullable | NULL means the queue, not "hidden". |
| `source` | **new** — `String(16)`, `NOT NULL DEFAULT 'pipeline'` | `pipeline` or `manual`, pinned by `ck_opportunities_source_known`. Mirrors `Client.source` and exists for the same reason that column gives: it must not be inferable from a nullable FK a retention purge can clear. There is no `shared` value — sharing never creates a row, so nothing could honestly carry it. |
| `email_message_id` | `nullable=False` → nullable; `CASCADE` → `SET NULL` | Both halves matter. Nullable admits manual job orders. `SET NULL` stops a retention purge of the mail body from deleting a job order that has been assigned, shared and worked on — the same argument `Client.first_seen_email_message_id` already makes. |

`ON DELETE SET NULL` on `assigned_user_id` is what happens when a recruiter
leaves: their job orders drop into the queue, visible and claimable, rather than
disappearing.

### `opportunity_shares`

One row per grant. Tenant-scoped, RLS-covered.

| Column | Notes |
|---|---|
| `opportunity_id` | composite FK `(tenant_id, opportunity_id)`, `ON DELETE CASCADE` |
| `scope` | `user` or `tenant`, pinned by CHECK |
| `shared_with_user_id` | composite FK, `SET NULL`. NULL if and only if `scope='tenant'` — enforced by `ck_opportunity_shares_scope_target`, the same paired-nullability idiom as `sex_requirement`/`sex_requirement_reason` |
| `shared_by_user_id` | composite FK, `SET NULL`. Who shared it must outlive the account that did |
| `note` | free text — "this one's yours, I'm on leave" |
| `created_at` | from `Timestamps` |

Two partial unique indexes, so re-sharing updates rather than duplicating:

- `(tenant_id, opportunity_id, shared_with_user_id)` where `scope = 'user'`
- `(tenant_id, opportunity_id)` where `scope = 'tenant'`

There is no `access` column. Every share is read.

### Migration

Three hazards, all on live data:

1. **Existing opportunities backfill to `assigned_user_id = NULL`**, which puts
   the entire existing pipeline into the queue. Under the rules above that is
   visible to everyone, so nothing disappears on deploy — this is precisely why
   the queue is visible rather than hidden. No backfill from mailbox owner is
   performed; assignment is a decision a person makes.
2. **`client_id` backfills** from each opportunity's email through
   `client_mentions`, which already records which client each message referred
   to. Rows whose mention is gone stay NULL.
3. **Both new tables need `ENABLE ROW LEVEL SECURITY` and `FORCE` policies in
   the migration itself**, or `verify_rls_enforced`
   ([rls.py](../../../backend/app/db/rls.py)) refuses to boot the service.

## Visibility

### The predicate

A single module, `backend/app/services/visibility.py`, exposing one function
that returns a SQLAlchemy boolean clause rather than a filtered query — so it
composes with the existing sort, search and pagination without `list_opportunities`
changing shape.

A user may see an opportunity when any of these holds:

1. `assigned_user_id IS NULL` — the unassigned queue
2. `assigned_user_id = :user_id` — theirs
3. a share exists with `scope = 'tenant'`
4. a share exists with `scope = 'user'` and `shared_with_user_id = :user_id`
5. they own the mailbox the source email arrived in — joined through
   `email_messages.mailbox_id → mailboxes.user_id`
6. `role = 'owner'`, which collapses the clause to TRUE

Term 6 uses the only elevated role that exists
([tenant.py](../../../backend/app/models/tenant.py), where a unique index already
guarantees at most one owner per tenant). No new role vocabulary is introduced.

Write access is separate and narrower:

```
can_edit = assigned_user_id IN (:user_id, NULL) OR role = 'owner'
```

Unassigned is editable because it must be claimable. Claiming is the only way
work leaves the queue besides an owner assigning it.

### Why not RLS

The tenant boundary is a hard, permanent one and belongs in the database, which
is where it is. The assignment boundary is an in-tenant product rule that will
move as the product moves; encoding it as an RLS policy would mean a migration
every time sharing semantics change, and policies that reference an
application-set `app.user_id` are only as strong as the code that sets it
anyway.

The cost of that choice is real and worth stating plainly: **this filter lives
in application code and can be forgotten.** Two things contain it — every by-id
read goes through one `load_visible_opportunity(...)` helper, and a test asserts
that no route in `opportunities.py` selects an `Opportunity` by id without it.

`load_visible_opportunity` raises **404, not 403**, when the predicate fails. A
403 confirms the row exists.

### The call sites that would leak

Four existing reads in `app/api/opportunities.py` fetch an opportunity by id and
would each expose a job order the caller cannot see:

| Function | What leaks |
|---|---|
| `list_opportunities` | the list itself |
| `_evidence_counts` | evidence for an invisible job order |
| `_decoded_codes` | decoded client codes — the sensitive ones |
| `get_eligibility` | eligibility for an invisible vacancy |

`opportunities.py` is 691 LOC. Sharing endpoints go in a new
`app/api/opportunity_shares.py` rather than growing it toward the 1500-line
limit.

## Ingestion

The pipeline already resolves **one client per email** — "three vacancies in one
mail come from one company"
([persist.py](../../../backend/app/services/ingest/persist.py)). So the client is
known at persist time and every opportunity from that email takes the same
`client_id`. No new matching logic is needed.

The chain, in order:

1. `match_client(...)` yields the client → `opportunity.client_id`
2. `opportunity.assigned_user_id = client.assigned_user_id`
3. Unmatched client, or matched but unassigned → NULL → the queue

The mailbox owner is never consulted for assignment. They keep read access
through term 5 of the predicate, and nothing more.

**`match_client` must complete before the opportunity rows are written**, so
`client_id` is available on INSERT. If the current call order in `persist.py`
writes opportunities first, that is a real code move rather than a field
addition.

**Assignment is written on INSERT only, never on re-extract.** `extract_email`
re-runs after a crash and replay appends — which is why `client_mentions`
carries its once-per-message unique constraint. If a replay recomputed
`assigned_user_id`, a recruiter who had claimed a queued job order would
silently lose it to a re-run.

**Manual creation** sets `source='manual'`, `email_message_id=NULL`,
`assigned_user_id` to the creator — you typed it in, it is yours, not the
client's assignee's — and `client_id` to whichever client the creator picked,
which may be NULL. Its id is a plain UUID; the existing `_opportunity_id` scheme
derives from email plus index and does not apply.

## Reassignment

When a client moves from one recruiter to another, the API offers to move the
open job orders with it, **defaulting to yes** — a client changing hands
normally means the work changes hands. It stays a choice rather than an
automatic cascade, because the outgoing recruiter may be mid-placement on one of
them and a silent bulk reassignment is invisible to everyone it affects.

Job orders already closed or filled never move. The response reports how many
rows changed, so the interface can say "12 job orders moved to Sarah" instead of
reassigning them quietly.

## API

All routes under `/api`; the session is resolved by `_require_session`
([auth.py](../../../backend/app/api/auth.py)). Every mutating route re-checks
permission server-side and none trusts a client-supplied `assigned_user_id`.

### `app/api/opportunity_shares.py` (new)

| Route | Behaviour |
|---|---|
| `POST /api/opportunities/{id}/shares` | Body `{scope, user_ids?, note?}`. The caller must be able to see the job order — a recipient may re-share. Upserts against the partial unique indexes, so re-sharing updates the note rather than returning 409. |
| `DELETE /api/opportunities/{id}/shares/{share_id}` | Permitted to the assignee, the original sharer, the owner, and to a recipient removing themselves. |
| `GET /api/opportunities/{id}/shares` | Who this is shared with. Visible-only. |

**Unsharing revokes sight, not history.** Notes, candidate submissions and
evidence the recipient created stay. Deleting them would assert the work never
happened — the same argument `client_mentions` makes for `SET NULL` over delete.

### Added to `app/api/opportunities.py`

| Route | Behaviour |
|---|---|
| `POST /api/opportunities/{id}/claim` | Only when `assigned_user_id IS NULL`. An atomic `UPDATE ... WHERE assigned_user_id IS NULL` resolves the race two recruiters will genuinely hit in a 9pm rush; the loser gets 409. |
| `POST /api/opportunities/{id}/assign` | Body `{user_id \| null}`. Assignee or owner. `null` releases it to the queue. |
| `POST /api/opportunities` | Manual create. |
| `GET /api/opportunities?scope=` | `mine`, `queue`, `shared_with_me`, `all`. Default `all`. These filter **within** visibility and can never widen it. |

### Added to `app/api/clients.py`

| Route | Behaviour |
|---|---|
| `PUT /api/clients/{id}/assignee` | Body `{user_id \| null, move_open_opportunities}` — defaulting to `true`. Reports the number moved. |
| `POST` / `DELETE /api/clients/{id}/collaborators` | Cover recruiters. |

## Notifications

`emit()` in `persist.py` is tenant-wide, and `OpportunityEvent` carries
`tenant_id` but no recipient. Every subscriber in the agency currently receives
every job order — correct when everyone could see everything, and a leak now.

**`OpportunityEvent` gains `recipient_user_ids: tuple[UUID, ...] | None`.**
`None` retains the current tenant-wide meaning, so nothing else in the catalogue
changes. `dispatch.py` intersects subscribers with recipients before writing
outbox rows. A tenant-level shared destination — the "one shared destination"
case in the notification design — still receives everything, because it is
subscribed at tenant scope rather than as a user.

The two existing kinds change behaviour:

| Kind | Recipients |
|---|---|
| `opportunity.new` | the assignee; everyone if unassigned |
| `opportunity.needs_review` | same |

Two new kinds, absorbed as constants with no migration, exactly as the
notification design anticipated:

| Kind | Recipients | Payload adds |
|---|---|---|
| `opportunity.shared` | the new recipients only | sharer's name, the note |
| `opportunity.assigned` | the new assignee only | who assigned it |

Claiming from the queue emits nothing — you did it, you know.

A tenant broadcast is **one event with N recipients**, not N events, so the
per-event hourly cap still applies per subscriber and a thirty-person agency
does not produce a burst that trips it wholesale. The cap was sized against a
forty-vacancy morning; a broadcast share is bounded by human clicking and needs
no limit of its own.

## Testing

- The visibility predicate, term by term, including an owner seeing a job order
  assigned to someone else and a mailbox owner seeing one assigned away from
  them.
- A cross-tenant share is refused by the composite foreign key, not merely by
  application code.
- Every by-id route in `opportunities.py` goes through
  `load_visible_opportunity` — asserted structurally, so a new route added later
  fails the test rather than leaking.
- An invisible job order returns 404, never 403.
- Re-extraction of an email does not overwrite a claimed `assigned_user_id`.
- Two concurrent claims: one 200, one 409.
- Re-sharing to the same user updates the note and does not raise.
- Unsharing leaves the recipient's notes and submissions intact.
- Client reassignment moves open job orders and leaves closed ones.
- Deleting a user drops their job orders into the queue rather than deleting
  them.
- A shared job order notifies only its recipients, and a tenant broadcast
  produces one event, not N.
