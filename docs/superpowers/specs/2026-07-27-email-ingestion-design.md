# Email Inbox Ingestion → Structured Job Data

**Date:** 2026-07-27
**Covers:** plan Stage 3 (Data Ingestion) + Stage 4 (AI Extraction)
**Plan sections:** §2.3, §6.2, §7–§16, §17, §18, §19, §30, §32

## Goal

A recruiter connects Outlook. Every job-order email that arrives from then on
becomes a row of structured recruitment data — company, position, salary,
working hours, requirements, duration, location, and the date it was received —
without discarding the source email.

The service must be robust (no silent data loss), performant (a notification is
acknowledged in under three seconds), and light (no infrastructure beyond what
is already provisioned).

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Scope | End to end: ingest **and** extract | Ingestion alone produces nothing a user can look at |
| Queue | Redis + arq (Upstash, already provisioned) | Mature retry/scheduling; poll interval tuned to ~2–5 s and pool capped, because Upstash bills per command |
| Raw body storage | Cloudflare R2 (already provisioned) | Keeps large HTML bodies out of Postgres; metadata stays relational |
| Body metadata | Postgres `email_messages` | Queryable, RLS-covered, indexed for dedup |
| Company/contact | Plain strings on `opportunities` | Normalising into entities (§17) carries its own dedup problem; nothing in the target output needs it yet |

### Deliberately out of scope

- **Opportunity-level deduplication** (§19, "possible duplicate / updated
  vacancy"). Needs real data to tune similarity thresholds against.
- **Embeddings / semantic search** (§20). Same reason.
- **Attachments.** Referenced and recorded (`has_attachments`), not parsed.

Both deferrals are choices, not oversights. Revisit once a few thousand real
emails exist.

## Architecture

```text
Outlook mail arrives
   │
   ▼
Graph change notification ──► POST /api/graph/notifications
   │                            • echo validationToken (text/plain) on create/renew
   │                            • verify clientState
   │                            • resolve tenant via graph_subscriptions
   │                            • INSERT email_messages (status=pending)
   │                            • enqueue fetch_email
   │                            • 202 in < 3 s
   ▼
arq: fetch_email ──► Graph GET /messages/{id}  (Prefer: IdType="ImmutableId")
   │                  • body_text + body_html → R2 (deterministic key)
   │                  • UPDATE received_datetime, sender, subject, r2 keys
   │                  • status = fetched
   │                  • enqueue extract_email
   ▼
arq: extract_email ─► preprocess (HTML→text, §11)
   │                  • LLM fast model, JSON schema (§12, §13)
   │                  • 0..N opportunities (§16) + extraction + evidence (§14)
   │                  • status = extracted | needs_review
   ▼
Postgres: opportunities
```

Two jobs, not one. A Graph throttle on fetch must not cost an LLM call; a bad
LLM response must not cost another Graph round trip. Each retries independently.

### Recovery layer

The existing supervisor in `app/workers/main.py` gains three periodic tasks.
It runs as a **separate process** from the arq worker — different lifecycle,
different failure mode.

| Task | Interval | Purpose |
|---|---|---|
| `renew_subscriptions` | 15 min | Graph message subscriptions have a maximum lifetime (currently 10080 minutes). Read the returned `expirationDateTime` rather than assuming a constant, and renew at under 50 % of remaining life; recreate on failure (§8) |
| `delta_sync` | 10 min | Per-mailbox `deltaLink` walk. Recovers missed notifications and webhook downtime (§9). Also marks deleted messages `gone` |
| `rescan_stuck` | 5 min | Re-enqueues any row stalled in a non-terminal status — `pending` older than 5 min, and `fetched` or `extracting` older than 15 min |

`rescan_stuck` must sweep every non-terminal status, not just `pending`. A
worker killed after the status flips to `fetched`, or mid-`extracting`, would
otherwise strand the row forever and falsify success criterion 3.

Redis cannot join a Postgres transaction, so the enqueue-after-commit gap is
real. `rescan_stuck` closes it. Dedup indexes make every replay a no-op.

## Schema

### Routing layer

**`mailboxes`** — tenant_id, user_id, ms_user_id, folder_id, status
(`active` | `needs_reauth` | `disconnected`), delta_link, initial_sync_from
(§6.2: today / 3 days / 7 days / custom), timestamps.

**`graph_subscriptions`** — tenant_id, mailbox_id, subscription_id, resource,
expires_at, last_renewed_at, status, timestamps.

`graph_subscriptions` is read **policy-exempt**. Graph notifications are lean —
they carry a message id and a subscription id and nothing else — and the webhook
is unauthenticated, so this table is the only way to learn which tenant a
notification belongs to. It is the routing table, and the lookup necessarily
precedes any tenant context.

### Raw layer (§10, §2.3)

**`email_messages`** — tenant_id, mailbox_id, graph_message_id (immutable-ID
form), internet_message_id, conversation_id, sender_name, sender_email, subject,
**received_datetime**, has_attachments, body_r2_key, body_html_r2_key,
processing_status, attempt_count, last_error, timestamps.

`processing_status`: `pending` → `fetched` → `extracting` → `extracted` |
`needs_review` | `failed` | `gone`.

Unique on `(tenant_id, mailbox_id, graph_message_id)` **and** `(tenant_id,
mailbox_id, internet_message_id)`. Both identifiers, because Graph's `id`
changes when a message moves folders — that is precisely why
`Prefer: IdType="ImmutableId"` is set on both the subscription and the fetch,
and why §19 names both.

Scoped to `mailbox_id` deliberately. Two recruiters at one agency CC'd on the
same job email share an `internet_message_id`; a tenant-wide constraint would
silently discard the second recruiter's copy and with it the fact that they
received it. Each mailbox keeps its own row. Recognising that those rows
describe one vacancy is opportunity-level dedup (§19), which this spec defers.

R2 key: `{tenant_id}/{mailbox_id}/{immutable_message_id}.{txt,html}`.
Deterministic, so a retry overwrites rather than orphans. The object is written
**before** the status flips to `fetched`; a crash between the two leaves a
harmless duplicate write on retry, never a row pointing at nothing.

### Extracted layer

**`opportunities`** — one row per vacancy, so a single email carrying three jobs
produces three rows (§16). tenant_id, email_message_id, company_name, job_title,
job_description, salary_min, salary_max, salary_currency, salary_raw,
working_hours, requirements, employment_duration, work_location, and
received_datetime denormalised from the email for sorting and filtering.

Every field preserves the `Not mentioned` distinction (§15): a nullable column
plus the raw string the model returned. "The model found nothing" and "the model
was not asked" must stay distinguishable.

**`extractions`** — one row per LLM run: model name, model version, prompt
version, token counts, latency, overall confidence, raw JSON response. Enough to
replay every extraction when the prompt improves (§14).

**`extraction_evidence`** — per field: field name, extracted value, confidence,
and the source text span it came from. This is what makes "the AI must not
fabricate" (§15) auditable rather than aspirational, and it is what the review
queue (§21) renders.

## Multi-tenancy under RLS

Every business table carries `tenant_id` via the `TenantScoped` mixin (§18).
The runtime role is RLS-bound and has no `BYPASSRLS`.

Background work has no HTTP request and therefore no session tenant. The
resolution:

1. The webhook (unauthenticated) reads `graph_subscriptions` through a
   policy-exempt path to map `subscription_id` → tenant, user, mailbox.
2. Every arq job body runs inside `app/workers/tenant_context.py`, which issues
   `SET LOCAL` on the tenant GUC for the transaction. Nothing touches a business
   table outside that wrapper.

`verify_rls_enforced()` already runs at worker startup
(`app/workers/main.py:64`); it proves policies exist but never sets context.
The wrapper is what supplies it.

## Token lifecycle

Delegated refresh tokens die — revocation, 90-day inactivity, or a CAE event.
When a refresh fails:

- mailbox → `needs_reauth`
- the Graph subscription is deleted rather than left to expire noisily
- the user is prompted to reconnect

No silent retry loop. §8 calls this "detects disconnected accounts".

Entra rotates refresh tokens on use, so two concurrent refreshes for one user
lose one of them. Refresh is serialized per user with a Postgres advisory lock.

Graph also sends **lifecycle notifications** (`reauthorizationRequired`,
`subscriptionRemoved`) to a separate `lifecycleNotificationUrl`. Registering it
is strongly recommended for mailbox resources — without it, a revoked grant
surfaces only as notifications quietly stopping. It drives the same state
machine.

## Mailbox onboarding (§6.2)

Nothing above works until a mailbox exists and a `deltaLink` has been
established, so connecting a mailbox is part of this spec:

1. User completes Microsoft sign-in with mailbox read scope.
2. User chooses where ingestion begins — today, last 3 days, last 7 days, or a
   custom date — and optionally a folder. Never download the whole mailbox.
3. Insert the `mailboxes` row with `initial_sync_from` and `status = active`.
4. **Initial sync job:** walk the delta endpoint filtered from
   `initial_sync_from`, inserting `email_messages` rows at `pending` and
   enqueueing `fetch_email` for each. Page through to the end and store the
   final `deltaLink`. This backfill is the same code path `delta_sync` uses;
   the only difference is the starting point.
5. Create the Graph subscription (and lifecycle URL); store `subscription_id`
   and the `expirationDateTime` Graph returns.

Order matters: the subscription is created **after** the initial delta walk
starts, so a message arriving during backfill is caught by one path or the
other. The dedup indexes make the overlap harmless.

A Google-only user reaches none of this and must be told so, rather than shown
an empty dashboard.

## Module layout

No file approaches the 1500-LOC limit; each is split by failure domain so it can
be tested against a fake.

```text
app/api/graph_webhook.py             POST /api/graph/notifications
                                     POST /api/graph/lifecycle
app/services/graph/client.py         Graph HTTP: ImmutableId header, 429 Retry-After,
                                     per-mailbox concurrency cap (Graph allows 4)
app/services/graph/subscriptions.py  create / renew / recreate (§8)
app/services/graph/delta.py          deltaLink sync + reconciliation (§9)
app/services/ms_auth.py              (exists) + per-user advisory-locked refresh
app/services/storage/r2.py           put/get body by deterministic key
app/services/ingest/preprocess.py    HTML→text, signature and disclaimer trim (§11)
app/services/ingest/extract.py       prompt build, LLM call, JSON-schema validate (§12, §13)
app/services/ingest/persist.py       opportunities + extraction + evidence, one transaction
app/workers/jobs.py                  arq: fetch_email, extract_email
app/workers/tenant_context.py        SET LOCAL tenant GUC wrapper
app/models/mailbox.py
app/models/graph_subscription.py
app/models/email_message.py
app/models/opportunity.py
app/models/extraction.py
```

All routes stay under `/api` — `tests/test_routing.py` fails otherwise, because
the static mount at `/` would shadow them.

New settings (repo-root `.env`, via `app.core.config.settings` — nothing
hardcoded): `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET_NAME`, `REDIS_URL` are already present. Add poll interval, retry
ceiling, and subscription renewal margin as settings rather than literals.

## Failure and retry matrix

| Failure | Behaviour |
|---|---|
| Enqueue fails after DB commit | `rescan_stuck` re-enqueues `pending` rows older than 5 min |
| Graph 429 / 503 on fetch | arq retry honouring `Retry-After`, exponential backoff, max 5 attempts |
| Message deleted before fetch (404) | status → `gone`, no retry. The source is genuinely lost; record it rather than hide it |
| R2 write fails | Retry. Status flips to `fetched` only after the object lands |
| LLM returns invalid JSON | One reprompt, then escalate to the strong model (§32), then `failed` |
| LLM low confidence | status → `needs_review`, row still written. A review queue, not a black hole |
| Refresh token dead | mailbox → `needs_reauth`, subscription deleted, user prompted |
| Webhook downtime | `delta_sync` recovers on the next pass; dedup indexes make replay a no-op |
| Duplicate notification | Unique-index conflict → `ON CONFLICT DO NOTHING`, job exits clean |
| Subscription expired unrenewed | `renew_subscriptions` recreates it; `delta_sync` backfills the gap |

## Testing

- Fake Graph client fixture returning canned notification and message payloads.
- R2 faked with an in-memory dict.
- LLM faked with recorded responses.

The full pipeline — notification through to `opportunities` rows — runs in tests
with zero network access. Tests run against a throwaway Postgres container;
`tests/conftest.py` refuses a non-local host.

Extraction quality is measured separately by a golden-file eval set (§39 Stage
4), seeded from real recruitment emails. Pipeline correctness and extraction
accuracy are different questions and get different test suites.

## Success criteria

1. An email arriving in a connected mailbox appears as an `opportunities` row
   without manual action.
2. The webhook returns 202 in under three seconds.
3. Killing the arq worker mid-flight loses no email — it is re-enqueued.
4. Every extracted field is traceable to a source span and a model version.
5. Every stored email is replayable from R2 after a prompt change.
6. Agency A cannot read Agency B's rows, verified by test.
