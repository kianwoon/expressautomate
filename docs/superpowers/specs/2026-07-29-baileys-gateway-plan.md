# Baileys WhatsApp Gateway — Decision Document

Decided 2026-07-29. Baileys (`WhiskeySockets/Baileys`) gateway so a recruiter
sends from their own number without leaving expressautomate. The choice is
made; this document decides the build.

Naming rule up front, because two WhatsApp integrations now coexist:

| Concern | Name everywhere | Settings prefix | Tables |
|---|---|---|---|
| Meta Cloud API notifications to recruiters (existing) | "WhatsApp notifications" | `WHATSAPP_*` | `whatsapp_suppressions` |
| Baileys per-recruiter outbound (this build) | **"WA gateway"** / `wa_gateway` | `WA_GATEWAY_*` | `wa_sessions`, `wa_session_keys` |

No file, setting, or table from one side ever uses the other's prefix.
`backend/app/api/whatsapp_webhook.py` is untouched.

## 1. Where the gateway lives

New top-level **`gateway/`** — Node 22 + TypeScript, because Baileys is a JS
library and wrapping it from Python is fantasy. Layout:

```
gateway/
  package.json  tsconfig.json  Dockerfile
  src/index.ts          # Fastify HTTP server on :7300
  src/auth.ts           # shared-secret check + Postgres-backed Baileys auth state
  src/sessions.ts       # SessionManager: one Baileys socket per recruiter
  src/routes.ts         # POST /sessions/{user_id}/start|logout, GET /sessions/{user_id}, POST /send
  src/crypto.ts         # AES-256-GCM around auth-state values
```

It talks to the same Postgres (own role `wa_gateway_role`, GRANTed only on the
two `wa_*` tables — it must never be able to read candidates or emails) and is
called only by FastAPI; the browser never reaches it.

Deploy: third Koyeb service **`gateway`** in the same app, same pattern as
`api`/`worker`/`arq`. `.github/workflows/backend.yml` gains a `gateway` build
scoped to `gateway/**` (mirroring the existing per-service scoping), plus
`koyeb service update expressautomate/gateway`. New FastAPI code lives in
`backend/app/api/wa_gateway.py` (routes under `/api/wa/…`) and
`backend/app/services/wa_gateway.py` (HTTP client to the gateway).

## 2. Statefulness

**Exactly one instance, `scale 1`, no autoscaling ever** — a Baileys socket is
process-local; two instances would fight over the same WhatsApp session and
get both logged out. Koyeb deploys are rolling, so for a window two instances
exist: the gateway takes an advisory lock per session
(`pg_advisory_lock(hashtext(user_id))`) before opening a socket, so the old
instance's sockets die on SIGTERM before the new one acquires them.

On deploy every socket drops. This is acceptable **because auth state is
persisted**: Baileys' `AuthenticationState` (creds + signal key store) is
written to Postgres on every `creds.update`, so the new instance calls
`makeWASocket` with the stored state and resumes **without a QR**. Reconnect
of all known sessions runs at boot. User-visible effect of a redeploy:
status flips to `reconnecting` for ~5–30 s; the send path falls back to the
popup (see §7) meanwhile. No spinner lies: the UI shows the real status.

## 3. Credential storage & encryption

Baileys auth state is `creds` (one JSON object) plus a keyed store
(`pre-key`, `session`, `sender-key`, `app-state-sync-key`, … each id → blob).
Schema (Alembic migration in `backend/`, models in
`backend/app/models/wa_session.py`):

- `wa_sessions(id, tenant_id, user_id UNIQUE, phone_e164, status, status_detail, qr_expires_at, last_connected_at, sent_today int, sent_date date, created_at, updated_at)` — TenantScoped, RLS like every business table.
- `wa_session_keys(session_id FK CASCADE, category text, key_id text, value_encrypted bytea, updated_at, PRIMARY KEY(session_id, category, key_id))` — `creds` stored as category `'creds'`, key_id `''`.

Encryption: **AES-256-GCM per value**, random 12-byte nonce prepended,
AAD = `session_id:category:key_id` (binds ciphertext to its row — no
cross-user splicing, which is the §18 teeth here). Key: new setting
**`WA_GATEWAY_ENCRYPTION_KEY`** (32 bytes base64) in root `.env`, alongside the
existing `TOKEN_ENCRYPTION_KEY` precedent; the gateway reads it from its own
env. Rotation: versioned key — first byte of `value_encrypted` is a key
version; `WA_GATEWAY_ENCRYPTION_KEY_PREVIOUS` decrypts old rows, rewrite
happens lazily on next write. Plaintext never leaves the gateway process;
FastAPI can read the tables but not decrypt (it never gets the key — set the
env var on `gateway` only). **Nothing session-related is ever returned to the
browser except the QR string and the status.**

## 4. Trust between FastAPI and the gateway

Koyeb's service mesh gives every service a private internal address
(`gateway.<app>.internal:7300`) reachable only inside the app — deploy the
gateway **without a public route** so it is not publicly callable at all.
Belt and braces: every request also carries
`Authorization: Bearer ${WA_GATEWAY_SHARED_SECRET}` (new setting, both
services), checked with a constant-time compare, because "private networking"
is a Koyeb config detail that could drift exactly like the Route setting in
CLAUDE.md. Verify the internal DNS name against Koyeb docs during Phase 1; if
mesh addressing differs, the shared secret already carries the security and
only the URL changes (`WA_GATEWAY_URL` setting — never hardcoded).

## 5. QR pairing flow

1. Recruiter clicks "Connect my WhatsApp" in dashboard settings →
   `POST /api/wa/session` → FastAPI calls gateway `POST /sessions/{user}/start`.
2. Gateway emits QR strings via Baileys' `connection.update`; it POSTs each
   fresh QR to FastAPI's internal callback `POST /api/wa/internal/status`
   (shared-secret-guarded), which stores status + current QR (QR kept
   **in-process/short-lived**, not in the DB) and publishes on the existing
   SSE bus (`backend/app/api/events.py`) as event `wa_session`.
3. Browser: already-connected `frontend/app/events.ts` stream delivers the
   nudge; the settings page refetches `GET /api/wa/session`, which returns
   `{status, qr?}` and renders the QR client-side (`qrcode` npm pkg).
   QR strings rotate ~every 20 s; each nudge → refetch → new QR. No polling.
4. Never scans: Baileys stops issuing QRs after ~5 attempts (~2 min); gateway
   reports `disconnected`, UI shows "Pairing timed out — try again" with a
   retry button. Abandoned pairing stores nothing durable.

## 6. Connection state machine

Statuses on `wa_sessions.status`, CHECK-constrained:
`pairing → connected → reconnecting → disconnected | logged_out`.

- **Writer: the gateway only**, always via the FastAPI internal callback (so
  FastAPI is the single DB writer and the SSE publisher). Sources:
  `connection.update` (open→`connected`, close+`loggedOut`→`logged_out`,
  close otherwise→`reconnecting` then `disconnected` after 3 failed retries).
- API knows a session died two ways: the pushed callback, plus a liveness
  sweep in the existing supervisor (`backend/app/workers/tasks.py`, new
  periodic task) that calls gateway `GET /sessions/{user}` every 60 s and
  marks `disconnected` any session the gateway doesn't recognize (covers a
  crashed gateway that never got to push).
- UI learns via the `wa_session` SSE event; the candidates page also gets
  session status embedded in the whatsapp-draft response so the send modal
  knows which path it will take before the click.

## 7. Send path

`POST /api/candidates/{id}/whatsapp-send {message}` → FastAPI checks
`wa_sessions.status == 'connected'` → gateway `POST /send {user_id, jid,
text}` → gateway returns the WhatsApp message id → FastAPI writes the
activity row (§8) and returns 200.

**Not connected (or gateway 5xx/timeout): agree with the recommendation,
emphatically.** The modal in `candidate-whatsapp.tsx` keeps its existing
popup button always available; "Send via expressautomate" appears only
when the session reports `connected`, and a send that fails at request time
returns a typed error the modal answers with "Couldn't send — open in
WhatsApp Web instead", pre-filled popup one click away. A dead gateway
degrades to Step 1, which already shipped and works. The popup path still
writes `status='opened'` exactly as today.

## 8. `candidate_activities` evolution

New migration widens the CHECKs (`5c2d8e14f6a1` stays as history):
`_ACTIVITY_TYPES += ('whatsapp_sent',)`, `_STATUSES += ('sent','failed')`,
and adds `provider_message_id text` + `error text` columns. No backfill —
existing `opened` rows keep meaning what they meant.

§15 honesty, stated in the model docstring and the migration comment:
- `sent` means **the gateway's socket accepted the message and WhatsApp
  returned a message id**. It does **not** mean delivered, and never read —
  we deliberately do not ingest delivery receipts in v1, so no `delivered`
  or `read` status exists in the vocabulary yet. Adding them later requires
  actually observing receipts (Baileys `messages.update`), a new migration,
  and nothing else may write them.
- `failed` means the gateway returned an error; `error` holds it verbatim.
- `opened` keeps its exact current meaning (popup path).

## 9. Rate limiting & ban-risk mitigation (day one)

- **Per-recruiter daily cap**: `WA_GATEWAY_DAILY_LIMIT` (default 50) enforced
  in FastAPI against `wa_sessions.sent_today` (reset when `sent_date` rolls);
  429 with a human message, popup fallback still offered.
- **Minimum send spacing**: ≥30 s ± jitter between sends per session.
  **Amended in P5: a send inside the window is refused, not queued.** The API
  returns 429 with the seconds remaining and the popup stays available.
  Queueing was the original design and it is wrong here: an in-process queue
  holds messages across a redeploy, where they are either dropped or sent
  twice, and a candidate receiving the same message twice is the outcome this
  whole section exists to avoid. Making a queue safe needs an outbox table and
  a resume-after-restart dispatcher — a subsystem, not a phase.
  The jitter is drawn **once, when a send is admitted**, and stored as
  `wa_sessions.next_send_allowed_at`. Re-rolling it on refusal would mean a
  recruiter who waits exactly the number we quoted can be refused again, and
  the UI shows that number verbatim.
- **No bulk endpoint.** One candidate, one click, one message — the API shape
  itself prevents blast campaigns in v1.
- **Text only** in v1 (no media): smaller surface, lower spam signature.
- Send only to numbers the recruiter already holds as candidates (jid derived
  server-side from the candidate record, never from the browser).
- Onboarding screen restates the ban risk and requires an explicit checkbox
  per recruiter before first pairing (recorded on `wa_sessions`).

**Two things P5 did not build, recorded so they are not mistaken for done:**

- **The liveness half of the sweep.** The sweep resolves stale `pending` rows
  to `unknown`, which was the part that mattered — a send whose outcome nobody
  will ever learn must not sit as `pending` forever. It does **not** yet
  re-check session status in the background, so `last_checked_at` means only
  "when something last asked", which is what the column says and therefore
  still honest. The consequence is that a session WhatsApp has dropped is
  noticed when a recruiter next uses it rather than before.
- **The consent checkbox** above. Nothing yet records that a recruiter was
  told about the ban risk before pairing. The settings panel explains what
  linking does, but explaining is not recording, and if this is meant to be
  evidence that they were warned, it does not exist yet.

## 10. Phased delivery (each phase shippable, product working throughout)

**P1 — Gateway skeleton + deploy** (`gateway/*`, Dockerfile, workflow scoping,
Koyeb service, health endpoint, shared secret): **Opus** — new service,
cross-cutting CI/deploy surgery on a workflow that has bitten twice.
**P2 — Schema + crypto + auth-state store** (migration, `wa_session.py`
models, `src/crypto.ts`, `src/auth.ts`, RLS tests): **Opus** —
security-sensitive key handling and §18.
**P3 — Pairing + state machine** (`src/sessions.ts`, `wa_gateway.py` API +
internal callback, SSE event, settings-page UI): **Sonnet** — standard
plumbing over decided design; recon of `events.ts` internals by **Haiku**
Explore first.

P3 carries two requirements from the P2 review, both about the connection
pool it will be the first to open. Neither is optional.

- **Assert at boot that the role cannot bypass RLS**: `SELECT rolbypassrls
  FROM pg_roles WHERE rolname = current_user`, and refuse to start when it is
  true. The store scopes every statement with `set_config('app.tenant_id', …)`
  and leans on the policy to enforce it; against a bypassing role the policy
  is decorative and §18 quietly stops being true. Today the URL points at
  `expressautomate_app`, which is NOBYPASSRLS — but that is a fact about a
  Koyeb secret nobody in this repo can see, so the process has to check.
- **Set `ssl` explicitly.** The production DSN carries `?sslmode=require`
  against a Koyeb certificate in no trust store. The Python side maps that to
  encrypt-without-verify on purpose; `node-postgres` does not read `sslmode`
  the same way, so a pool built from the URL alone will not connect.

One thing to know before touching `gateway/`: the image tag hashes the whole
directory, tests included, so a test-only edit still redeploys the gateway and
drops every live socket. Once real sessions exist, that is a reason to batch
gateway changes rather than trickle them.

**Amended by P3: sessions restore lazily, not at boot.** §2 said the gateway
would restore every session when it starts. That cannot be written. Both
session tables are FORCE RLS scoped on `app.tenant_id`, and `tenants` is
RLS'd on its own id, so with no tenant set a query sees nothing anywhere —
there is no way to enumerate "every tenant holding a session" without a
bypass role, and the boot assertion added in this same phase exists precisely
to refuse one. The two requirements were in direct conflict and the RLS one
wins.

What happens instead: a session resumes from stored credentials the moment it
is first asked about, and never asks for a fresh QR to do it. The property
P2 was built to guarantee still holds; only the trigger moved, from a boot
sweep to first touch.

**P5's kill-and-restart test must therefore ask.** Restarting the process and
waiting proves nothing now — nothing wakes a session on its own. The test has
to restart, then call `GET /api/wa/session` for a known session, and assert
that it comes back `connected` without a QR.
**P4 — Send path + activities migration + modal fallback**
(`whatsapp-send` endpoint, CHECK-widening migration, `candidate-whatsapp.tsx`):
**Sonnet** — 1–3 file changes per side, design fully specified here.
**P5 — Rate limits, daily caps, liveness sweep, redeploy-resume test**:
**Sonnet**, with the resume-without-QR integration test reviewed by **Opus**
(it guards the riskiest property).
Ship gate each phase: existing popup flow still green.

## 11. Riskiest thing

**Auth-state persistence corruption**: Baileys mutates signal keys on nearly
every event; miss one `creds.update`/key write (or write them out of order)
and the restored session decrypts nothing — WhatsApp silently logs the device
out, every recruiter re-pairs, and repeated re-pairing is itself a ban signal.
The catch: an integration test in `gateway/` that pairs a real test number,
kills the process mid-conversation, restarts, and asserts (a) no QR is
requested and (b) a message sends — run manually before each release of
`gateway/**` (documented in `gateway/README` as a release gate), plus a unit
test that every category Baileys requests from the key store round-trips
through the encrypted Postgres store byte-identically.

## 12. CLAUDE.md deploy-section tripwires (specific)

1. **"First call to an external system" rule fires twice.** The `gateway`
   service needs `DATABASE_URL`-equivalent, `WA_GATEWAY_ENCRYPTION_KEY`,
   `WA_GATEWAY_SHARED_SECRET` set **by hand on Koyeb** — a brand-new service
   starts with zero env vars, the exact `GRAPH_BASE_URL`/`R2_*` failure mode.
   And `api` gains its first call to the gateway: `WA_GATEWAY_URL` +
   `WA_GATEWAY_SHARED_SECRET` must be added to `api`'s env. Run the
   CLAUDE.md `koyeb deployment get … env[].key` check for **both** services
   before shipping P1 and P3.
2. **Route/health-check drift**: the new service must be created with **no
   public route** and health check `GET /health` on :7300 — set by hand,
   not in repo; record both in the CLAUDE.md deploy table when P1 lands.
3. **Workflow scoping**: `backend.yml`'s diff-scoping logic must learn
   `gateway/**`, or a gateway-only commit takes the "deploy both services"
   fallback and needlessly recycles `api`/`worker`/`arq` — dropping every
   live socket for no reason. Also: the gateway must **not** run on the
   shared backend image; it gets its own image tag keyed on `gateway/**`.
4. **Scale**: Koyeb autoscaling must stay pinned at 1 for `gateway` — another
   hand-set value; add it to the same deploy table.
