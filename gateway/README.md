# WA gateway

Per-recruiter outbound WhatsApp via Baileys. Design:
[docs/superpowers/specs/2026-07-29-baileys-gateway-plan.md](../docs/superpowers/specs/2026-07-29-baileys-gateway-plan.md).

Not to be confused with the Meta Cloud API **WhatsApp notifications** in
`backend/` (`WHATSAPP_*`). Nothing here uses that prefix.

## Status: P1 — skeleton only

What exists: an HTTP server, an unauthenticated `GET /health`, a shared-secret
guard on everything else, and one stub route (`GET /status`) that exists so the
guard is exercised. **Baileys is not a dependency yet and no session, socket or
database code exists.** P2 has landed: `wa_sessions`/`wa_session_keys`,
`src/crypto.ts`, `src/store.ts` and the Baileys auth state in `src/auth.ts` can
persist and restore a session — but **nothing calls them yet**. P3 wires them
to a socket and adds pairing.

## Develop

```bash
npm ci
npm test            # typecheck + node:test (store tests skip without a database)
scripts/test-db.sh  # provisions wa_gateway_test on ea-test-db:5433, then npm test
npm run build       # tsc -> dist/ (tests excluded)
```

The store tests need a real Postgres — `ON CONFLICT`, the `bytea` round trip
and RLS are Postgres behaviours, and a double would agree with whatever bug the
store has. `scripts/test-db.sh` creates its **own** database (never sharing the
backend suite's) and brings it to head with the real Alembic migrations, so
schema drift fails locally rather than on Koyeb.

## Environment

Set by hand on the `gateway` Koyeb service; a new service starts with none, the
`GRAPH_BASE_URL`/`R2_*` failure mode in the root `CLAUDE.md`. The process
refuses to start if any required one is missing.

| Variable | Required | Notes |
|---|---|---|
| `WA_GATEWAY_SHARED_SECRET` | yes | Same value on `api` (plan §4). |
| `WA_GATEWAY_DATABASE_URL` | yes | Gateway's own restricted Postgres role. |

**Two things about that URL, for whoever builds the pool in P3.**

It must be a role *without* `BYPASSRLS`. `PostgresAuthStore` scopes every
statement with `set_config('app.tenant_id', …)` and relies on the row-level
policy to enforce it; a bypassing role makes the policy decorative and §18
stops being true. In production it is the Koyeb secret `ea-database-url`,
the same `expressautomate_app` role `api` uses.

And it carries `?sslmode=require` against a Koyeb certificate that is not in
any system trust store — the reason `settings.asyncpg_connect_args` on the
Python side maps `require` to encrypt-without-verify rather than passing it
through. `node-postgres` does not read `sslmode` the same way, so the pool
this store is handed will need its `ssl` option set deliberately. Nothing
here does that yet, because nothing here opens a connection yet.
| `WA_GATEWAY_ENCRYPTION_KEY` | yes | 32 bytes, base64. **`gateway` only** — giving it to `api` defeats the point. |
| `WA_GATEWAY_ENCRYPTION_KEY_PREVIOUS` | no | Only while a key rotation drains. |
| `WA_GATEWAY_PORT` / `WA_GATEWAY_HOST` | no | Default 7300 / 0.0.0.0. |

## Release gate (from P2 onward, plan §11)

The riskiest property is auth-state persistence: miss one `creds.update` and a
restored session decrypts nothing, WhatsApp silently logs the device out, and
every recruiter re-pairs — which is itself a ban signal. Before releasing any
change under `gateway/**` once sessions exist, run the manual integration test
by hand: pair a real test number, kill the process mid-conversation, restart,
and assert (a) no QR is requested and (b) a message still sends.

## Deploy

Own image (`ghcr.io/kianwoon/expressautomate-gateway`), own Koyeb service,
**no public route, scale pinned to 1**. See the deploy table in the root
`CLAUDE.md` — those settings live nowhere in this repo.
