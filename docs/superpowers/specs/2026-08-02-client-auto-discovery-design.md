# Client auto-discovery from mailbox headers (Phase 1)

**Date:** 2026-08-02 · **Source plan:**
`ExpressAutomate_Client_Auto_Discovery_Implementation_Plan_Phase1.md` ·
**Status:** implemented

## What this is

A Settings function that scans the signed-in recruiter's own mailbox —
headers only — for the configured lookback window (90 days by default),
aggregates business sender domains, and:

1. **backfills contacts onto clients that already exist** (the pipeline has
   never created a `client_contacts` row — every client today has zero), and
2. **offers the ranked NEW domains** for one-click creation as `confirmed`
   clients, each with its contacts.

Zero LLM calls. Nothing is written to `email_messages`. The delta-token
ingestion path is untouched: this scan uses plain date-range paging on
`GET /me/messages` and `GET /me/mailFolders/sentitems/messages` with
`$select` limited to `from,receivedDateTime` / `toRecipients,sentDateTime`.
Roughly 200 clients' worth of signal is extracted from thousands of emails
for the price of header pages, which is the efficiency complaint this
feature answers.

## Confirmed mapping (user-approved)

| Graph datum | Lands on |
|---|---|
| unique sender email | `client_contacts.email` |
| sender display name | `client_contacts.name` (falls back to the address itself — never invented, §15) |
| domain → company | `clients.email_domain`, via a discovery upsert on the same partial unique index the pipeline uses |
| top sender per domain | `client_contacts.is_primary` (only when the client has no primary yet) |

No new source-of-truth tables. `client_discovery_runs` (below) is an
ephemeral per-user run/cache row, not a system of record — clients and
contacts remain the only durable output.

## Decisions

Fixed by the user, not revisited: signed-in user's mailbox only; the ranked
list shows **new** domains only; selected companies are created
**confirmed**; the function lives under Settings as its own tab.

Made here:

- **Enrichment of existing clients is automatic on scan.** It is
  non-destructive (only inserts contacts that are not already there, only
  sets a primary where none exists, only moves `last_seen_at` forward) and it
  is literally what "backfill onto existing data" asks for. Gating it behind
  selection would add a decision the user has no reason to make.
- **Results survive as JSONB on a `client_discovery_runs` row** (TenantScoped,
  RLS-policied, one live row per user — a new scan deletes the previous
  runs). Redis/arq job results expire and are awkward to read from the API
  process; a run row survives restarts, is tenant-isolated by the same
  policy as everything else, and mirrors `sourcing_runs`, the repo's
  existing "start, poll, read back" idiom.
- **The scan runs in the arq worker with polling**, not inline. Real paging
  math: a 90-day window on a working recruiter's mailbox is thousands of
  inbox headers plus sent items; at `$top=100` that is dozens-to-hundreds of
  sequential Graph round trips (~200–400 ms each), i.e. 30 s–2 min — far past
  what a browser request or Koyeb's proxy should hold open. The POST answers
  202 with the run; the panel polls GET, exactly as sourcing does.
- **No Sent Items scope change is needed.** `MS_MAILBOX_SCOPES=Mail.Read`
  covers the whole mailbox, Sent Items included — ingestion only ever read
  the inbox because the *folder* is pinned to `inbox`, not because of the
  grant. Nothing about the onboarding initial-sync window changes (see
  "Deliberately left out").
- **Created clients mirror the manual-create endpoint exactly**:
  `status='confirmed'`, `source='manual'`, no assignee (assignment stays an
  explicit act, as everywhere else). `name` is the domain itself — resolving
  a company name from a domain is the source plan's Phase 2, and anything
  else here would be a guess (§15).
- **Merged domains follow the merge chain**, mirroring ingestion's matcher:
  a domain whose only client rows are merged is *not* "new" — its contacts
  enrich the surviving client, because that is where ingestion sends that
  domain's mail.
- **Sweep for abandoned runs.** A run left `pending` (its enqueue was lost —
  the row committed, the job never did) or `running` (a worker died before
  any exception handler could run) would otherwise sit unclaimed until the
  recruiter happened to scan again. The supervisor's
  `sweep_stale_client_discovery_runs` (a SECURITY DEFINER function, migration
  `20260812_1000_sweep_stale_client_discovery.py`) parks both in `failed`
  with words the recruiter can act on — the run is a user-facing button, not
  pipeline state, so the retry stays a click. The scan endpoint's own stale
  check (`CLIENT_DISCOVERY_STALE_RUNNING_MINUTES`) remains as the second line
  of defense and covers the gap before the sweep ticks. arq's own in-flight
  retry covers a worker killed mid-job (the claim accepts `running` again),
  and a throttle re-queues itself via `Retry(defer=retry_after)`.

## Exclusions (all configurable, nothing hardcoded)

A sender/recipient is counted only when every gate passes:

1. `domain_of()` — malformed addresses and `FREE_EMAIL_DOMAINS` (the existing
   client-identity rule, reused, not duplicated).
2. The scanning user's **own domain** (colleagues are not clients) — derived
   from their `users.email`, suffix-matched.
3. `CLIENT_DISCOVERY_EXCLUDED_DOMAINS` — job boards and bulk-mail
   infrastructure, suffix-matched (`bounce.linkedin.com` matches
   `linkedin.com`). Deliberately minimal: a noisy row costs an unticked
   checkbox, a silent exclusion hides a real client.
4. `CLIENT_DISCOVERY_SYSTEM_LOCALPARTS` — `noreply@`, `postmaster@`, … An
   entry matches the lowercased local part (`+tag` stripped) exactly, or as
   a prefix whose next character is not a letter (`noreply1`,
   `newsletter-team`).

## Ranking

Per domain, from the source plan, weights from settings:

```
score = received × W_RECEIVED (1)
      + sent × W_SENT (2)
      + unique_contacts × W_UNIQUE_CONTACTS (5)
      + RECENCY_BONUS (10) if last_activity within RECENCY_DAYS (14)
```

`sent` counts one per message per domain (a message to three people at
acme.com is one interaction; all three still become contacts). Contacts per
domain are ranked by message count, then recency, then address; the run
stores at most `CLIENT_DISCOVERY_MAX_CONTACTS_PER_CLIENT` per domain and at
most `CLIENT_DISCOVERY_MAX_DOMAINS` domains (flagged when truncated, never
silently short). The walk itself stops after
`CLIENT_DISCOVERY_MAX_MESSAGES` headers, whole-page like the delta walk.

## Pieces

| Piece | Where |
|---|---|
| Settings block + `.env.example` | `CLIENT_DISCOVERY_*` in `app/core/config.py` |
| Run row | `app/models/client_discovery.py` → `client_discovery_runs`, migration `20260802_1000_client_discovery_runs.py` with the standard FORCE-RLS policy |
| Scan/rank/apply service | `app/services/client_discovery.py` |
| arq job `run_client_discovery` | `app/workers/discovery_jobs.py`, registered in `app/workers/settings.py` with `CLIENT_DISCOVERY_JOB_TIMEOUT_SECONDS` |
| API | `app/api/client_discovery.py`: `POST /api/client-discovery/scan` (202), `GET /api/client-discovery` (latest run for this user), `POST /api/client-discovery/clients` (create selected) |
| UI | Settings tab "Client discovery": `frontend/app/settings/client-discovery/` |

The scan POST authenticates through the same `_connected_user` helper the
mailbox preview uses, so "no mailbox grant" and "Google-only account" get
the same honest 403s they get there. The GET and the create POST need only
a session — reading our own row and writing clients touch no Graph.

Contact application takes a per-tenant advisory lock
(`client-discovery-apply`) so a double-click or a concurrent scan cannot
insert duplicate contacts; client creation itself is already idempotent on
the domain's partial unique index (`ON CONFLICT … DO UPDATE` that promotes
`unconfirmed → confirmed` and never touches any other status).

## Security / tenancy

`client_discovery_runs` carries `tenant_id`, the standard
`tenant_isolation` FORCE-RLS policy, and a composite `(tenant_id, user_id)`
FK to `users` (CASCADE — a run is ephemeral and dies with its user). Every
read in the API is via `tenant_session`; a foreign run id is simply absent.
Results are additionally filtered per user (`user_id = me`) because a scan
of *my* mailbox is mine to act on.

## Test plan

- Service: exclusion gates (free/system/own/excluded/suffix), aggregation
  and paging over faked Graph pages (`httpx.MockTransport`), score weights
  read from settings, truncation flags, contact ranking.
- Job: end-to-end over a mock transport — enrichment adds missing contacts
  only, sets a primary only where none exists, new domains land in results;
  `MailboxNotAuthorised` → run `failed` with a reconnect message.
- API: 401/403 gates, run lifecycle (scan → 409 while fresh, supersede when
  stale), enqueue-failure marks the run failed, create path (created /
  already_existed / not_in_scan), RLS + per-user isolation.
- Route manifest regenerated; `api.contract.test.ts` covers the new path
  helpers automatically.
- Frontend: panel states and the create flow under a stubbed `fetch`.

## Deliberately left out (proposals, not implementations)

- **Onboarding initial-sync window unchanged.** If anything, discovery makes
  a *shorter* ingestion window more tolerable (clients no longer depend on
  ingesting 90 days of bodies), but changing it was not approved and is not
  needed by this feature.
- No continuous background re-scan, no company-name resolution, no logo
  fetch — the source plan's later phases.
- No SSE nudge for run completion; a poll on the settings page is enough
  and mirrors sourcing.
