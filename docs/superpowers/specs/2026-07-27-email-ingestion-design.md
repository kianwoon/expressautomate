# Email Inbox Ingestion → Structured Job Data

**Date:** 2026-07-27
**Covers:** plan Stage 3 (Data Ingestion) + Stage 4 (AI Extraction)
**Plan sections:** §2.3, §6.2, §7–§16, §17, §18, §19, §30, §32
**Revision:** 2 — incorporates external design review (see *Review outcomes*)

## Goal

A recruiter connects Outlook. Every job-order email that arrives from then on
becomes a row of structured recruitment data — company, position, salary,
working hours, requirements, duration, location, and the date it was received —
without discarding the source email while that source is legitimately retained.

The service must be robust (no silent data loss), performant (a notification is
acknowledged in under three seconds), and light (no infrastructure beyond what
is already provisioned).

Guiding principle: **the email is the source; trusted structured recruitment
data is the asset.**

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Scope | End to end: ingest **and** extract | Ingestion alone produces nothing a user can look at |
| Queue | Redis + arq (Upstash, already provisioned) | Mature retry/scheduling; poll interval tuned to ~2–5 s and pool capped, because Upstash bills per command |
| Raw body storage | Cloudflare R2 (already provisioned) | Keeps large HTML bodies out of Postgres; metadata stays relational |
| Body metadata | Postgres `email_messages` | Queryable, RLS-covered, indexed for dedup |
| Relevance gate | Cheap classifier before extraction | A work mailbox is mostly not job orders; classifying is far cheaper than extracting |
| Company/contact | `*_raw` + `*_normalized` strings on `opportunities` | Analytics-ready without committing to an entity model and its dedup problem |
| Retention | Configurable, default 24 months | Amends §2.3 — see *Retention* |

### Deliberately out of scope

- **Opportunity-level deduplication** (§19, "possible duplicate / updated
  vacancy") and **embeddings / semantic search** (§20). Both need real data to
  tune thresholds against.
- **A controlled taxonomy** for `job_family` and `seniority`. Columns exist and
  are nullable; a later pass populates them once there is data to shape the
  vocabulary. Asking the LLM to invent categories now produces drift, not
  analytics.
- **Attachments.** Referenced and recorded (`has_attachments`), not parsed.
- **The review-queue UI.** This spec creates the data model that makes review
  possible (`review_status`, human overrides); the interface is §21.

All four are choices, not oversights.

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
   │                  • enqueue classify_email
   ▼
arq: classify_email ─► cheap model: is this a recruitment job order?
   │                    • recruitment | uncertain → enqueue extract_email
   │                    • non_recruitment → status `skipped`; body purged after
   │                      a short retention window (default 7 days)
   ▼
arq: extract_email ─► preprocess (HTML→text, §11)
   │                  • LLM fast model, JSON schema (§12, §13)
   │                  • 0..N opportunities (§16) + extraction + evidence (§14)
   │                  • deterministic evidence validation
   │                  • status = extracted (or no_opportunity)
   ▼
Postgres: opportunities  ──► search · analytics · export
```

Separate jobs, not one. A Graph throttle on fetch must not cost an LLM call; a
bad LLM response must not cost another Graph round trip; a non-recruitment email
must not cost an extraction. Each retries independently.

### Recovery layer

The existing supervisor in `app/workers/main.py` gains periodic tasks. It runs
as a **separate process** from the arq worker — different lifecycle, different
failure mode.

| Task | Interval | Purpose |
|---|---|---|
| `renew_subscriptions` | 15 min | Graph message subscriptions have a maximum lifetime (currently 10080 minutes). Read the returned `expirationDateTime` rather than assuming a constant; renew at under 50 % of remaining life; recreate on failure (§8) |
| `delta_sync` | 10 min | Per-mailbox `deltaLink` walk. Recovers missed notifications and webhook downtime (§9), and updates `source_state` |
| `rescan_stuck` | 5 min | Re-enqueues any row stalled in a non-terminal status — `pending` older than 5 min, `fetched` / `classifying` / `extracting` older than 15 min |
| `purge_expired` | daily | Strips bodies past their retention horizon — see *Retention* for exactly what is deleted |

`rescan_stuck` must sweep every non-terminal status. A worker killed after the
status flips to `fetched`, or mid-`extracting`, would otherwise strand the row
forever and falsify success criterion 3.

Redis cannot join a Postgres transaction, so the enqueue-after-commit gap is
real. `rescan_stuck` closes it without distributed transactions. Dedup indexes
make every replay a no-op.

## Schema

### Routing layer

**`mailboxes`** — tenant_id, user_id, ms_user_id, `scope` (`whole_inbox` |
`folder`), folder_id, folder_name, status (`active` | `needs_reauth` |
`disconnected`), delta_link, initial_sync_from, retention_months, timestamps.

Agencies often keep a dedicated `Jobs` or `Client Requirements` folder.
Monitoring one folder is cheaper and less privacy-invasive than the whole inbox,
so folder scope is offered at onboarding rather than bolted on later.

**Both scopes resolve to exactly one folder id.** Graph's message delta is
folder-scoped, so `whole_inbox` is not a distinct mechanism — it resolves to the
well-known `Inbox` folder at onboarding and is stored as a folder id like any
other. Subscription resource and delta resource are then derived identically,
and there is one code path rather than two that look like one. Mail filed into a
subfolder of Inbox leaves the monitored scope and surfaces as
`removed_from_folder`, which is why that state exists. Enumerating every folder
in a mailbox is explicitly not in scope.

**`graph_subscriptions`** — tenant_id, mailbox_id, subscription_id, resource,
`client_state`, expires_at, last_renewed_at, status, timestamps.

`client_state` is generated per subscription, not shared. Graph echoes it on
every notification, and comparing it is the only thing standing between the
public webhook and anyone who can guess the URL. One global secret makes every
tenant's notifications forgeable if it ever leaks; a per-subscription random
value limits the blast radius to one mailbox. Compare in constant time.

`graph_subscriptions` is the routing table: Graph notifications are lean — a
message id and a subscription id, nothing else — and the webhook is
unauthenticated, so this is the only way to learn which tenant a notification
belongs to, and the lookup necessarily precedes any tenant context.

It is **not** exempt from RLS. `verify_rls_enforced()` in `app/db/rls.py` fails
startup for any table the runtime role can read that lacks FORCE ROW LEVEL
SECURITY, deliberately and by structure rather than by a table allowlist — so an
exempt table would not boot. The narrow exemption is instead a
`SECURITY DEFINER` function owned by the migration role:

```sql
resolve_subscription(subscription_id text)
  RETURNS TABLE (tenant_id uuid, mailbox_id uuid, client_state text)
```

The table keeps its policy; one function with one argument and three returned
columns is the entire pre-tenant surface. A leak there exposes routing ids, not
mail.

### Raw layer (§10, §2.3)

**`email_messages`** — tenant_id, mailbox_id, graph_message_id (immutable-ID
form), internet_message_id, conversation_id, sender_name, sender_email, subject,
**received_datetime**, has_attachments, body_r2_key, body_html_r2_key,
processing_status, source_state, classification_status, classification_reason,
classification_model, classification_version, retention_until, attempt_count,
last_error, timestamps.

Three orthogonal state machines, deliberately not collapsed into one column:

```text
processing_status   pending → fetched → classifying → extracting →
                    extracted | no_opportunity | skipped | unfetchable | failed

source_state        present | removed_from_folder | deleted | unknown

classification_status  unknown | recruitment | non_recruitment | uncertain
```

The five terminal values of `processing_status` are distinct outcomes, and
`rescan_stuck` ignores all of them: `extracted` (vacancies written),
`no_opportunity` (a recruitment email containing no vacancy), `skipped` (the
classifier said non-recruitment — the pipeline mirror of
`classification_status = non_recruitment`), `unfetchable` (the message vanished
before any body was stored), and `failed` (we broke, and it is worth alerting
on). Collapsing `skipped` or `unfetchable` into `failed` would make the failure
rate meaningless.

`processing_status` describes *our* pipeline. `source_state` describes the
mailbox. Conflating them was a bug in revision 1: Graph's message delta is
folder-scoped, so an `@removed` event means "no longer in the monitored folder",
which is *usually a move, not a deletion*. Once the source is safely in R2, a
recruiter filing an email into a subfolder must not invalidate the opportunity
extracted from it. Only a confirmed deletion sets `deleted`; a `404` on fetch
before any body was stored sets `deleted` too, because that source really is
gone.

Per-opportunity review state lives on `opportunities`, not here — one email can
contain three jobs of which only one is doubtful (review comment #10).

Unique on `(tenant_id, mailbox_id, graph_message_id)` **and** `(tenant_id,
mailbox_id, internet_message_id)`. Both identifiers, because Graph's `id`
changes when a message moves folders — hence `Prefer: IdType="ImmutableId"` on
both the subscription and the fetch, and why §19 names both.

Scoped to `mailbox_id` deliberately. Two recruiters at one agency CC'd on the
same job email share an `internet_message_id`; a tenant-wide constraint would
silently discard the second recruiter's copy and with it the fact that they
received it. Each mailbox keeps its own row. Recognising those rows as one
vacancy is opportunity-level dedup (§19), which this spec defers.

R2 key: `{tenant_id}/{mailbox_id}/{immutable_message_id}.{txt,html}`.
Deterministic, so a retry overwrites rather than orphans. The object is written
**before** the status flips to `fetched`; a crash between the two leaves a
harmless duplicate write on retry, never a row pointing at nothing.

### Extracted layer

**`opportunities`** — one row per vacancy, so a single email carrying three jobs
produces three rows (§16). Analytics-ready from the start, because retrofitting
dimensions onto a year of rows is far worse than carrying nullable columns now.

```text
tenant_id, email_message_id, received_datetime   -- denormalised for sorting/filtering

company_name_raw, company_name_normalized
job_title_raw,   job_title_normalized
job_family, seniority                            -- nullable; later taxonomy pass

job_description
requirements
skills                                           -- text[]
industry

employment_type                                  -- permanent | contract | temp | unknown
work_arrangement                                 -- onsite | hybrid | remote | unknown
working_hours_raw

salary_min, salary_max, salary_currency, salary_period, salary_raw
duration_raw, duration_months
location_raw, location_normalized

review_status                                    -- ready | needs_review | approved | rejected
quality_state                                    -- verified | likely | needs_review
```

`salary_period` is not optional. `SGD 6,000`, `SGD 400`, `SGD 45` and
`SGD 90,000` are not comparable quantities without knowing whether they are per
month, day, hour, or year — and every salary analytic in §25 depends on that
comparison. Values: `hour | day | month | year | unknown`.

`work_arrangement` is a separate column rather than text buried in
`working_hours_raw`. "Hybrid (Fri WFH)" is a working-hours string *and* an
analytical dimension; only one of those survives if they share a column.

Every field preserves the `Not mentioned` distinction (§15): a nullable column
plus the raw string the model returned. "The model found nothing" and "the model
was not asked" must stay distinguishable.

**`extractions`** — one row per LLM run: tenant_id, **email_message_id**, model
name, model version, prompt version, token counts, latency, raw JSON response,
created_at. Keyed on the email, not the opportunity, because a run that finds
three vacancies is still one run — and a run that finds none must still be
recorded. Replaying a prompt upgrade appends a row; nothing is updated in place,
so the extraction history of any email is the ordered set of its rows (§14).

**`extraction_evidence`** — extraction_id, **opportunity_id**, field name,
extracted value, the source text span, `start_char`, `end_char`, model
confidence, and `evidence_valid`.

**`opportunity_field_overrides`** — opportunity_id, field_name, ai_value,
human_value, corrected_by, corrected_at. The effective value of a field is
`human_value` where one exists, otherwise the AI value.

## Evidence validation (review comment #2)

Asking a model for evidence is not verification — the model can fabricate the
evidence string as readily as the value. So the extraction contract requires
character offsets, and application code checks them:

```python
source[start_char:end_char] == evidence   # whitespace-normalised comparison
```

If the span does not match, `evidence_valid = false` and the field is forced to
`needs_review`. This is what turns "the AI must not fabricate missing values"
(§15) from a prompt instruction into an enforceable invariant. A prompt can be
ignored by a model; a string comparison cannot.

## Extraction quality (review comment #3)

A model's self-reported `0.94` is not a calibrated probability and must not be
shown to a user as though it were, nor used alone to gate review. `quality_state`
is derived from several signals:

| Signal | Deterministic? |
|---|---|
| Evidence present | yes |
| Evidence span matches source | yes |
| Salary parses, currency present, no conflicting figure | yes |
| Company string appears in source | yes |
| JSON validates against schema | yes |
| No ambiguity flag raised by the model | no |
| Model confidence | no |

Users see `Verified` / `Likely` / `Needs review`. Raw confidence is retained on
`extraction_evidence` for evaluation and calibration work, not for display.

## Multi-tenancy under RLS

Every business table carries `tenant_id` via the `TenantScoped` mixin (§18).
The runtime role is RLS-bound and has no `BYPASSRLS`.

Background work has no HTTP request and therefore no session tenant:

1. The webhook (unauthenticated) calls `resolve_subscription()` to map
   `subscription_id` → tenant, mailbox, client_state.
2. Every arq job body runs inside the **existing** `tenant_session()` context
   manager in `app/db/rls.py`, which sets `app.tenant_id` transaction-locally
   via `set_config(..., true)`. Nothing touches a business table outside it.

No new tenant-context module is needed — revision 2 proposed
`app/workers/tenant_context.py` before checking, and `tenant_session()` already
does exactly this. `verify_rls_enforced()` runs at worker startup
(`app/workers/main.py:64`); it proves policies exist, and `tenant_session()`
supplies the context they compare against.

## Token lifecycle

Delegated refresh tokens die — revocation, 90-day inactivity, or a CAE event.
On refresh failure: mailbox → `needs_reauth`, the Graph subscription is deleted
rather than left to expire noisily, and the user is prompted to reconnect. No
silent retry loop. §8 calls this "detects disconnected accounts".

Token refresh is serialized per user with a Postgres advisory lock:

```text
acquire advisory lock → refresh → persist newest refresh token → release
```

The lock guards against concurrent refresh updates, refresh-token persistence
races, and inconsistent stored token state. It is not premised on a claim that
using one refresh token instantly invalidates the previous one (review comment
#9 — revision 1 overstated this).

### Lifecycle notifications

Registered at a separate `lifecycleNotificationUrl`; strongly recommended for
mailbox resources, since without it a revoked grant surfaces only as
notifications quietly stopping.

| Event | Action |
|---|---|
| `reauthorizationRequired` | Attempt silent reauthorisation; on failure mark mailbox `needs_reauth` |
| `subscriptionRemoved` | Recreate the subscription |
| `missed` | Run `delta_sync` for that mailbox immediately |

## Mailbox onboarding (§6.2)

1. User completes Microsoft sign-in with mailbox read scope. **This stores the
   token and nothing else** — no mailbox row, no subscription, no backfill.
2. **Preview.** `GET /api/mailbox/preview` reports the inbox: Graph's
   `totalItemCount`, the oldest message's date, and a live `$count` per offered
   window. Every figure is Graph's own; a window Graph refuses to count is
   offered without a number rather than with a guess.
3. User chooses where ingestion begins from those windows — *from now on*
   (import nothing historical, the default), 7 days, 30 days, or the configured
   lookback cap. `POST /api/mailbox/ingest` carries the answer.
4. Insert the `mailboxes` row with `status = active`.
5. **Create the Graph subscription** and store `subscription_id` plus the
   `expirationDateTime` Graph returns.
6. **Initial sync job:** walk the delta endpoint from `initial_sync_from`,
   inserting `email_messages` at `pending` and enqueueing `fetch_email`. Page to
   the end and store the final `deltaLink`. This is the same code path
   `delta_sync` uses; only the starting point differs.

The subscription is created **before** the backfill (review comment #7). Any
email arriving mid-onboarding is then caught by both paths rather than
potentially neither, and the dedup indexes make the overlap free. Revision 1 had
this the other way round and solved the same problem with more reasoning.

**Historical limit.** Graph delta filtering by `receivedDateTime` is not a bulk
export mechanism. The initial sync is capped by two configurable settings —
default 90 days lookback and 5,000 messages — and the UI must not offer a custom
date beyond the configured lookback. Whichever limit is hit first stops the walk
and the mailbox is marked backfilled from that point. Bulk historical import is a
separate feature with a different mechanism; the onboarding UI must not imply it
exists. `offered_windows()` therefore derives the choices from the configured
cap rather than listing them, so a period the backfill cannot honour is never
shown, and `/api/mailbox/ingest` validates against that same list — the cap is
real, not advisory.

**Folder scope is not yet asked for.** `mailboxes.scope`/`folder_id` already
carry it and every window is measured against the Inbox, so a folder picker is
a UI addition later, not a schema change. Mailboxes provisioned before this step
existed keep the 90-day window they were given; nothing migrates them.

## Retention (review comment #12)

§2.3's "never discard the source email" is restated as: **preserve source
provenance while the source is legitimately retained.** Indefinite retention of
recruiters' mailbox contents is not a defensible default, and no agency data
protection review would accept it.

| Class | Default | Behaviour |
|---|---|---|
| Recruitment email source | 24 months, per-tenant configurable | R2 objects deleted, `body_r2_key` / `body_html_r2_key` nulled. The `email_messages` row and derived `opportunities` survive |
| Non-recruitment email source | 7 days, configurable | R2 objects deleted, keys nulled. The row survives |
| Mailbox disconnected | — | Subscription deleted, ingestion stops; existing data retained until its own horizon |
| Tenant deleted | — | The only case that deletes rows: every row and every R2 object under `{tenant_id}/` |

**`purge_expired` never deletes an `email_messages` row.** It deletes R2 objects
and nulls the keys. The row is the dedup index entry — delete it and the next
delta walk re-ingests, re-classifies, and re-pays for an email the system
already decided about. Only tenant deletion removes rows, and at that point
there is nothing left to deduplicate against.

Non-recruitment bodies are kept for a short window rather than discarded on the
spot, because a classifier false negative is otherwise unrecoverable and
invisible: you cannot replay what you deleted, and nobody notices an email that
never appeared. Seven days covers the period a mistake is most likely to be
spotted while keeping steady-state storage and privacy exposure low.

`purge_expired` enforces every horizon. `retention_until` is stamped at write
time so purging never has to recompute policy over history.

## Module layout

No file approaches the 1500-LOC limit; each is split by failure domain so it can
be tested against a fake.

```text
app/api/graph_webhook.py             POST /api/graph/notifications
                                     POST /api/graph/lifecycle
app/services/graph/client.py         Graph HTTP: ImmutableId header, 429 Retry-After,
                                     per-mailbox concurrency cap (Graph allows 4)
app/services/graph/subscriptions.py  create / renew / recreate (§8)
app/services/graph/delta.py          deltaLink sync, source_state updates (§9)
app/services/ms_auth.py              (exists) + per-user advisory-locked refresh
app/services/storage/r2.py           put/get/delete body by deterministic key
app/services/ingest/preprocess.py    HTML→text, signature and disclaimer trim (§11)
app/services/ingest/classify.py      recruitment relevance gate
app/services/ingest/extract.py       prompt build, LLM call, JSON-schema validate (§12, §13)
app/services/ingest/evidence.py      offset verification, quality_state derivation
app/services/ingest/persist.py       opportunities + extraction + evidence, one transaction
app/services/retention.py            retention horizons and purge
app/workers/jobs.py                  arq: fetch_email, classify_email, extract_email
app/db/rls.py                        (exists) tenant_session() — reused, not rebuilt
app/models/{mailbox,graph_subscription,email_message,opportunity,extraction}.py
```

All routes stay under `/api` — `tests/test_routing.py` fails otherwise, because
the static mount at `/` would shadow them.

Configuration comes from the repo-root `.env` via `app.core.config.settings`;
nothing hardcoded. `R2_*` and `REDIS_URL` already exist. Add: arq poll interval,
retry ceiling, subscription renewal margin, classifier model, initial-sync
lookback days, initial-sync message cap, and the three retention horizons.

## Failure and retry matrix

| Failure | Behaviour |
|---|---|
| Enqueue fails after DB commit | `rescan_stuck` re-enqueues stalled rows |
| Graph 429 / 503 on fetch | arq retry honouring `Retry-After`, exponential backoff, max 5 attempts |
| Message deleted before any body stored (404) | `processing_status = unfetchable`, `source_state = deleted`. Terminal, not retried. The source is genuinely lost; record it rather than hide it |
| Message removed from monitored folder | `source_state = removed_from_folder`. Extracted opportunities remain valid |
| R2 write fails | Retry. Status flips to `fetched` only after the object lands |
| Classifier fails or is unsure | Treated as `uncertain` → proceeds to extraction. Failing open costs an LLM call; failing closed loses a job order |
| LLM returns invalid JSON | One reprompt, then escalate to the strong model (§32), then `failed` |
| Evidence span does not match source | Field `evidence_valid = false`, opportunity → `needs_review`. Row still written |
| No vacancy found in a recruitment email | `no_opportunity`. A successful outcome, not a failure |
| Refresh token dead | mailbox → `needs_reauth`, subscription deleted, user prompted |
| Webhook downtime | `delta_sync` recovers on the next pass; dedup indexes make replay a no-op |
| Duplicate notification | Unique-index conflict → `ON CONFLICT DO NOTHING`, job exits clean |
| Subscription expired unrenewed | `renew_subscriptions` recreates it; `delta_sync` backfills the gap |
| Replay after prompt upgrade | Writes a new `extractions` row; `opportunity_field_overrides` are never overwritten |

## Testing

- Fake Graph client returning canned notification, message, and delta payloads.
- R2 faked with an in-memory dict.
- LLM faked with recorded responses.

The full pipeline — notification through to `opportunities` rows — runs in tests
with zero network access. Tests run against a throwaway Postgres container;
`tests/conftest.py` refuses a non-local host.

Cases that must be covered explicitly, because each is a bug this spec exists to
prevent: worker killed at every non-terminal status; `@removed` for a moved
message not invalidating its opportunity; a fabricated evidence span forcing
`needs_review`; replay not clobbering a human override; a tenant unable to read
another tenant's rows.

Extraction quality is measured separately by a golden-file eval set (§39 Stage
4), seeded from real recruitment emails. Pipeline correctness and extraction
accuracy are different questions and get different test suites.

## Success criteria

1. A job-order email arriving in a connected mailbox appears as an
   `opportunities` row without manual action.
2. The webhook returns 202 in under three seconds.
3. Killing any worker mid-flight loses no email — it is re-enqueued.
4. Every extracted field is traceable to a verified source span and a model
   version.
5. Every retained email is replayable from R2 after a prompt change, and replay
   never overwrites a human correction.
6. A non-recruitment email costs no extraction call and no long-term body
   storage.
7. Agency A cannot read Agency B's rows, verified by test.

## Review outcomes

External review (2026-07-27) scored the revision-1 design 8.5/10 and raised
twelve points. Ten were adopted as written: relevance gate (#1), verified
evidence offsets (#2), derived quality state (#3), analytics dimensions (#4),
`source_state` (#5), initial-sync limit (#6), subscription before backfill (#7),
`missed` lifecycle handling (#8), advisory-lock rationale (#9), status
separation (#10), and human overrides (#11).

Two were adopted with a stated modification:

- **#12 retention** — adopted, and it amends §2.3 rather than merely extending
  it. Recorded here and in `CLAUDE.md`.
- **#1 classifier** — the gate is adopted, but non-recruitment bodies are purged
  after a short window rather than never stored, so a misclassification stays
  recoverable during the period it is likely to be noticed.

One item in #4 was narrowed: `job_family` and `seniority` are stored but left
unpopulated until a controlled vocabulary exists. Asking a model for free-form
categories produces values that cannot be aggregated, which is the opposite of
the analytics goal.
