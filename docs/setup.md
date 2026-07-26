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

- `alembic upgrade head` → `alembic_version`, `tenants`, `users` created
- `GET /health` → `{"status":"ok","env":"development"}`
- `GET /health/db` → `{"status":"ok","database":"expressautomate"}`
