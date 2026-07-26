# Setup & infrastructure

## Provisioned

| Resource | Value | Source |
|---|---|---|
| Postgres | `expressautomate` DB on Koyeb instance `draftproof-db` (Postgres 16.14, region `was`) | created 2026-07-27 |
| Koyeb API | `KOYEB_TOKEN` | reused from `draftproof_services` |
| Cloudflare | account `019aea60…`, API token | reused |
| Container registry | GHCR (`GHCR_USERNAME` / `GHCR_PAT`) | reused |
| AI | OpenRouter key + base URL | reused |
| Object storage | Cloudflare R2, bucket `expressautomate` | endpoint reused, **bucket not yet created** |
| Redis | Upstash `current-boar-109865` | reused, shared |

`pgvector` 0.8.0, `pg_trgm`, and `uuid-ossp` are all available on the instance
for the Phase 2 semantic-search work (§20) — no extension is enabled yet.

Secrets generated fresh for this project (not shared with draftproof):
`APP_SECRET_KEY`, `TOKEN_ENCRYPTION_KEY` (32-byte, Fernet-compatible, encrypts
stored OAuth refresh tokens per §30), `MS_WEBHOOK_CLIENT_STATE`.

## Blocked — needs you

### 0. Sign-in providers — Microsoft **and** Google

Decided 2026-07-27, and it amends plan §6.1, which assumes Microsoft only.

Worth being explicit about the split, because the two are not interchangeable:

- **Identity** (who you are) — Microsoft or Google. Either can sign a user in.
- **Mailbox access** (what we ingest) — Microsoft Graph only. The product
  ingests Outlook mail; there is no Gmail ingestion path in the plan.

So a Google-only user can sign in, create a tenant and invite colleagues, but
will have nothing to ingest until someone connects a Microsoft 365 mailbox.
The onboarding flow must handle that state honestly rather than leaving them
on an empty dashboard — either prompt to connect Outlook, or say plainly that
Google sign-in does not yet carry mail ingestion.

Google side needs credentials you must create (Google Cloud Console →
APIs & Services → Credentials → OAuth client ID → Web application), then:

```
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
```

Authorised redirect URI: `https://expressautomate.app/auth/google/callback`
(plus `http://localhost:8000/auth/google/callback` for development). Scopes
`openid email profile` only — no Gmail scope, since we do not read Google mail.

### 1. Microsoft Entra ID app registration

Nothing in Stage 2 can be built without this, and only you can create it.

1. Azure Portal → **Microsoft Entra ID** → **App registrations** → **New registration**
2. Name `expressautomate.app`; supported accounts: **Accounts in any organizational directory** (multi-tenant — each agency is a tenant)
3. Redirect URI (Web): `http://localhost:8000/auth/microsoft/callback`
4. **Certificates & secrets** → new client secret → copy the *Value*
5. **API permissions** → Microsoft Graph → *Delegated* → add exactly:
   `openid`, `profile`, `email`, `User.Read`, `Mail.Read`, `offline_access`

   Do **not** add `Mail.ReadWrite`, `Mail.Send`, or any Application permission —
   read-only access is a product promise (§6.1).

Then fill in the repo-root `.env`:

```
MS_CLIENT_ID=<Application (client) ID>
MS_CLIENT_SECRET=<secret Value>
```

### 2. Domain

`expressautomate.app` is **not** registered in your Cloudflare account — only
`draftproof.app` is (expires 2027-05-01). Register it on Cloudflare Registrar,
then the DNS records to Koyeb can be wired up. Development runs on the free
`*.koyeb.app` subdomain until then.

### 3. Graph webhook URL

Microsoft Graph change notifications require a **public HTTPS** endpoint, so
webhook ingestion (§7) cannot be tested from localhost. Options: deploy the
backend to Koyeb early and point `MS_WEBHOOK_NOTIFICATION_URL` at it, or use a
tunnel for local development.

## Local development

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Verify:

```bash
curl localhost:8000/health/db
```

## Verified 2026-07-27

- `alembic upgrade head` → `alembic_version`, `tenants`, `users` created,
  `users_tenant_id_fkey` present with `ON DELETE CASCADE`
- `GET /health` → `{"status":"ok","env":"development"}`
- `GET /health/db` → `{"status":"ok","database":"expressautomate"}`
- `uv run pytest` → 5 passed · `uv run ruff check .` → clean

## Known follow-ups

- **Row-level security is not yet in place.** `tenant_id` + the FK are the only
  isolation today. RLS policies (§18, §30) should land before the first
  business tables (emails, opportunities) are created — retrofitting them
  across a populated schema is far more work.
- **TLS to Postgres is encrypt-without-verify.** Koyeb's server certificate is
  not in the system trust store, so `sslmode=require` maps to `CERT_NONE`.
  Pin Koyeb's CA and move to `verify-full` before production traffic.
- Tests run against the shared `expressautomate` database and clean up after
  themselves. Give CI its own database before running them concurrently.
