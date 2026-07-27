# Notification system — WhatsApp and Telegram

Decided 2026-07-28. Extends the plan's worker layer (§7); the event catalogue
draws on ingestion (§16, §17).

A recruiter learns that a job order arrived by opening the dashboard. Nobody
opens a dashboard at 9pm, which is when the job orders arrive. This system
pushes the two events that matter to a channel recruiters already watch all
day, and lets each person choose which events reach them and where.

## Decisions

| Question | Answer | Why |
|---|---|---|
| WhatsApp transport | Meta Cloud API direct | No middleman markup; we accept the WABA verification and template-approval setup as one-time cost. |
| Events in v1 | New opportunity extracted; opportunity needs review | Both already exist in the pipeline. Admin events (re-auth, sync failure) are deliberately deferred. |
| Preference scope | Per user *and* per tenant | A recruiter subscribes personally; an agency can also point one shared destination at the same catalogue. |
| Volume control | Immediate send, hourly per-event cap | A 40-vacancy morning is 40 billable WhatsApp messages otherwise. |
| WABA ownership | expressautomate owns one | A five-person agency will not complete Meta business verification to receive notifications. |
| Linking | Telegram deep-link token; WhatsApp opt-in code | A typed identifier that is one digit wrong delivers a client's job orders to a stranger. |

Deferred, and absorbed later as new `event_kind` constants with no migration:
digests, quiet hours, per-tenant WABA, admin events, email and SMS channels.
`frontend/` is empty, so this ships as backend and API; the settings UI is
separate work.

## Architecture

A new package, `backend/app/services/notify/`. Each module has one job and can
be read without the others.

| Module | Responsibility | Depends on |
|---|---|---|
| `events.py` | Event-kind constants and payload dataclasses. No I/O. | nothing |
| `dispatch.py` | Event → subscribers → outbox rows → enqueue | DB, `workers.queue.enqueue` |
| `render.py` | Payload → per-channel content | `events` |
| `channels/base.py` | `Channel` protocol: `async send(address, content) -> SendResult` | — |
| `channels/telegram.py` | Telegram Bot API | `base`, config |
| `channels/whatsapp.py` | Meta Cloud API | `base`, config |
| `linking.py` | Issue and redeem verification tokens | DB, `core.crypto` |

Around it: `app/api/notifications.py` (preference CRUD and linking),
`app/api/telegram_webhook.py`, `app/api/whatsapp_webhook.py`, and a
`deliver_notification` job in `app/workers/jobs.py`.

The producer knows nothing about channels. `services/ingest/persist.py` calls
`emit(OpportunityCreated(...))` and returns. Which channels exist, who
subscribed, and whether the send succeeded are all questions answered
downstream — otherwise adding a third channel means editing the ingestion
pipeline, which is where the reasoning about channels has no business being.

The webhook modules follow `app/api/graph_webhook.py`: validate the signature,
do the smallest durable thing, enqueue the rest.

## Data model

Four tenant-scoped tables, plus one that is deliberately global. The first four
carry `tenant_id` via `TenantScoped` and are covered by RLS; the fifth is
argued for on its own terms below.

### `notification_destinations`

Where messages go.

- `user_id` — nullable. Null means the destination belongs to the tenant rather
  than to a person: the agency's shared feed.
- `channel` — `telegram` or `whatsapp`.
- `address_encrypted` — the Telegram `chat_id` or the E.164 number, encrypted
  through `app.core.crypto`. A phone number is PII and does not belong in
  plaintext in a column an analytics query might select.
- `address_hash` — deterministic hash, because an encrypted column cannot carry
  a unique index. Unique on `(tenant_id, channel, address_hash)` — **per
  tenant**, not global: the same recruiter's number can legitimately appear
  under two agencies, and a global constraint would make the second one fail to
  link with no explicable reason.
- `verified_at`, `disabled_at`, `failure_count`.

### `notification_subscriptions`

Which events reach which destination. Unique on `(destination_id, event_kind)`,
plus an `active` flag. This table *is* the event-by-channel matrix the user
picks in the UI; there is no second representation of it.

### `notification_link_tokens`

Proof of ownership. Stored hashed, single-use, TTL'd from
`NOTIFY_LINK_TOKEN_TTL_MINUTES`. A token stored in the clear is a token that
leaks from a database backup into someone else's job orders.

### `notification_deliveries`

The outbox, and the design's load-bearing table. Columns: `destination_id`,
`event_kind`, `subject_id` (the opportunity), `status`
(`pending` | `sent` | `failed` | `suppressed`), `provider_message_id`,
`attempts`, `error`, timestamps.

It earns its place three times:

1. **Outbox.** Redis cannot join the Postgres transaction that committed the
   opportunity, so there is an unavoidable gap between "the event is durably
   recorded" and "a job exists to deliver it". `workers/queue.py` already
   documents this and fails soft rather than raising; the recovery sweep is
   what closes the gap. A notification with no row would simply be lost.
2. **Dedupe key.** `(destination_id, event_kind, subject_id)` answers "did we
   already tell this person about this vacancy" without keeping state anywhere
   else.
3. **Rate-cap counter.** See below — the cap is a query over this table, not a
   counter that can drift from it.

Ordering is: insert the outbox rows and commit, *then* enqueue. Never the
reverse — a job that starts before its row is committed reads nothing.

### `whatsapp_suppressions` — deliberately not tenant-scoped

Meta's opt-out (a user replying STOP) and quality rating attach to the *phone
number*, and we operate one shared number. "This person opted out" is therefore
a global fact about our WABA, not a fact about one agency. A tenant-scoped
table structurally cannot express it: agency B would keep messaging someone who
opted out through agency A, and Meta would count that against the number every
tenant shares.

So this table is keyed on `address_hash` alone, with no `tenant_id` and no RLS
policy. It is written only by the WhatsApp webhook and read only by the send
path. Nothing else may touch it — the absence of tenant scoping is a
correctness requirement here and a bug anywhere else, so it carries a comment
saying exactly that.

The shared number has a second consequence worth stating plainly: one tenant's
send behaviour moves the quality rating for every tenant. If one agency's
recipients block the number, everyone's throughput drops.

## Delivery

### Rate cap and rollup

Before sending, count this destination's deliveries for this `event_kind` in
the trailing hour. Over `NOTIFY_RATE_CAP_PER_HOUR`, the row is marked
`suppressed` rather than sent.

Suppressed rows are not discarded. The next message that *does* go out to that
destination counts them and appends "+N more since HH:MM", so the recruiter
knows the quiet hour was a cap and not an empty inbox.

That alone leaks its tail: if no further event arrives, the last suppressed
batch is never mentioned. The recovery sweep therefore has two duties — flush
outbox rows still `pending` past their window, *and* flush suppressed rows
older than the cap window as a rollup message. One sweep, both problems; no
new scheduler.

### Job idempotency

The sweep and the original enqueue can both fire for the same row. The job
claims its work before doing it:

```sql
UPDATE notification_deliveries SET status = 'sending'
WHERE id = :id AND status = 'pending'
RETURNING *
```

No row returned means another worker has it; the job exits. Claiming *after*
the send would double-message.

The job payload carries `tenant_id`. A worker has no ambient tenant, so without
it there is nothing to open a tenant-scoped session with. The sweep, which runs
outside any one tenant, iterates tenants explicitly the way `rescan_stuck`
does.

### WhatsApp is template-only

Every message here is business-initiated outside any 24-hour customer service
window, which under Meta's per-message pricing means a pre-approved **utility**
template — ordered `{{n}}` parameters, no free-form text. Telegram has no such
constraint and gets full formatting; the two renderers are genuinely different
and `render.py` does not pretend otherwise.

Three templates, submitted once:

| Purpose | Category |
|---|---|
| New opportunity | utility |
| Opportunity needs review | utility |
| Link verification code | authentication |

The first two carry a CTA URL button whose dynamic suffix is the opportunity
id. Template names and language come from `WHATSAPP_TEMPLATE_*` settings —
Meta's approval cycle means a template can be renamed or re-versioned without a
deploy, and a name compiled into source would need one.

### Failure handling

Transient — 5xx and 429 — retries with backoff, honouring `Retry-After`, up to
`NOTIFY_MAX_ATTEMPTS`. Past that the row is `failed` and stays that way; a
message about a vacancy from six hours ago is not worth a seventh attempt.

Permanent — Telegram `403` (bot blocked), WhatsApp undeliverable — does not
retry. It increments `failure_count` and disables the destination past
`NOTIFY_MAX_FAILURES`. A dead destination must become visible rather than
absorb messages forever.

Delivery outcomes and opt-outs arrive *inbound*, from Meta's webhook. Without
`whatsapp_webhook.py` the failure path has no input at all, so it is part of
v1, not a follow-up: `GET` answers `hub.challenge`, `POST` verifies
`X-Hub-Signature-256` against `WHATSAPP_APP_SECRET`.

## Linking

**Telegram.** The user is shown `t.me/<bot>?start=<token>`. The bot webhook —
authenticated by Telegram's `X-Telegram-Bot-Api-Secret-Token` header against
`TELEGRAM_WEBHOOK_SECRET` — resolves the token to a user and stores the
`chat_id`. Ownership is proved by the fact that only that person could have
pressed start.

**WhatsApp.** The user enters their number in the app; we send the
authentication template; they type the code back into the app. In-app consent
plus a returned code satisfies Meta's opt-in policy, and a code sent to a
mistyped number simply never comes back.

That send is an abuse surface — anyone could pump codes at arbitrary numbers on
our WABA's reputation. Unverified-number sends are rate-limited per user and
per source IP.

## Configuration

All from the repo-root `.env` via `app.core.config.settings`.

```
TELEGRAM_BOT_TOKEN, TELEGRAM_API_BASE_URL, TELEGRAM_WEBHOOK_SECRET
WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_API_BASE_URL
WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN
WHATSAPP_TEMPLATE_OPPORTUNITY_NEW, WHATSAPP_TEMPLATE_OPPORTUNITY_REVIEW
WHATSAPP_TEMPLATE_LINK_CODE, WHATSAPP_TEMPLATE_LANG
NOTIFY_RATE_CAP_PER_HOUR, NOTIFY_LINK_TOKEN_TTL_MINUTES
NOTIFY_MAX_ATTEMPTS, NOTIFY_MAX_FAILURES
NOTIFY_OPT_IN_MAX_PER_HOUR
```

Koyeb environment variables are set by hand per service and are not in this
repo. Every one of these must be applied to **both** the `api` service and the
worker. `GRAPH_BASE_URL` missing on `api` for a day is the precedent: harmless
until the first code path needed it, then every request 500ed.

## Testing

Channels sit behind the `Channel` protocol and are replaced by a fake in tests,
so nothing touches the network — the same seam `workers/queue.py` uses for
`_create_pool`.

Coverage that must exist:

- A destination in tenant A is invisible to tenant B under RLS.
- `whatsapp_suppressions` blocks a send regardless of which tenant initiated it.
- Cap arithmetic: N sends, cap exceeded, rows suppressed, next send carries the
  correct "+N more".
- The sweep flushes a suppressed batch when no further event arrives.
- Two concurrent workers claiming one delivery row produce one message.
- A link token cannot be redeemed twice and expires.
- Permanent failure disables the destination; transient failure retries.
- Template parameters are ordered correctly — a swapped `{{1}}`/`{{2}}` reads as
  a job title at a company that does not exist.
- Webhook signature rejection for both providers.
