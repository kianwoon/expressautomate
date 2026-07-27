# Setup & infrastructure

## Deployed

Repo: https://github.com/kianwoon/expressautomate (private). One Koyeb app,
`expressautomate`, with three services — all `eco-nano` (0.1 vCPU / 256 MB),
one replica, region `was`, the cheapest tier Koyeb offers.

| Service | Type | Route | Source |
|---|---|---|---|
| `web` | WEB | `/` | `frontend/` — Next.js static export behind nginx |
| `api` | WEB | `/api` | `backend/` — FastAPI |
| `worker` | WORKER | — | `backend/`, `python -u -m app.workers.main` |

Both public services sit behind the one domain, so the landing page calls the
API same-origin and needs no CORS.

### Route prefixes are stripped

**Koyeb removes the matched route prefix before forwarding.** A request to
`/api/health` reaches FastAPI as `/health`. So:

- API routes are declared **unprefixed** (`/health`, `/early-access`).
- `API_ROOT_PATH=/api` is set on the service so `/docs` and the OpenAPI schema
  advertise the public prefix.
- The service has exactly **one** route, `/api`. Several prefixes cannot share
  a single `root_path` — an earlier attempt with `/health`, `/docs` and
  `/openapi.json` as separate routes had each stripped differently and 404'd.

`backend/tests/test_routing.py` fails CI if a route is ever declared with the
`/api` prefix again.

**Running the frontend against a local backend:** the browser calls
`/api/early-access`, but locally nothing strips the prefix, so set
`API_ROOT_PATH=/api` in the repo-root `.env`. Starlette then strips it itself
and the paths line up with production. Leave it empty when running the API
alone.

### CI/CD

Two workflows with independent path triggers:

| Workflow | Triggers on | Deploys |
|---|---|---|
| `backend.yml` | `backend/**`, `.github/scripts/**` | `api` + `worker` |
| `frontend.yml` | `frontend/**` | `web` |

Cancellation is limited to pull requests. Run-level `cancel-in-progress` kills
in-progress jobs regardless of any job-level concurrency group, which could
interrupt a deploy between `alembic upgrade head` and the rollout. Each deploy
job instead checks it is still the tip of `main` and skips if superseded.

Both services sit in `was`, the same region as the Postgres instance, so
database round-trips stay in-datacentre.

**Watch memory.** `eco-nano` gives 256 MB and a FastAPI + SQLAlchemy + asyncpg
process idles near half of that. It was chosen to keep the starting bill down
and it is healthy today, but the first real ingestion load is the moment to
check for OOM restarts and move up to `eco-micro` (512 MB) if they appear.

### Pipeline timing

Measured on commit `6910051`: GitHub Actions **61s** (test 29s → deploy 25s,
sequential because deploy gates on tests), then Koyeb builds and rolls out,
reaching HEALTHY roughly **30s** later. Push to live is about **1.5 minutes**.

Koyeb layer-caches between deploys of the same service, so the archive rebuild
is much cheaper than a cold build — that was the main wrong assumption in the
first attempt at speeding this up.

### Image-based deploys (live)

Actions builds the backend image once with GHA layer caching, pushes it to
`ghcr.io/kianwoon/expressautomate-api`, and Koyeb pulls it. Test and build run
concurrently. Measured on `6c7cd42`: **52s** for the whole run — test 28s and
build 28s in parallel, deploy 20s.

Deployment uses `koyeb service update --docker`, **not** a raw API PATCH. A
PATCH carrying the full definition returns 2xx and leaves the image unchanged;
the deploy then reports success while production keeps running the previous
build. That happened here, silently, and is why every deploy now ends with
`.github/scripts/verify_koyeb.py` reading the state back and failing if the
running image is not the one this commit built.

Credentials: `GHCR_PAT` (GitHub Actions secret) pushes; the Koyeb registry
secret `ghcr-expressautomate` pulls. Both currently hold the `gh` CLI OAuth
token, which carries `write:packages`.

> **This token is not permanent.** `gh auth logout`, re-authenticating, or
> revoking the CLI's authorisation invalidates it, and deploys will fail on
> push (Actions) or pull (Koyeb) with a scopes error. For something durable,
> create a **classic** PAT with `write:packages` + `read:packages` and set it
> in both places — see below.

### Why `api` and `web` are separate services

Both are Koyeb **type** `WEB` — that only means "accepts HTTP and gets a
route". `web` is a service *name*. They are two services because frontend and
backend deploy independently, which needs two images.

They *can* be merged: FastAPI served the landing page before the split. One
Koyeb service is one image is one deploy unit, so the choice is independent
deploys **or** one instance, not both. Merging saves an `eco-nano` but makes
every landing-copy tweak restart the API.

### Replacing the GHCR credential

The remaining win is to stop Koyeb building at all: have Actions build once
with GHA layer caching, push to GHCR, and PATCH the services to the new image
tag. That is what `draftproof-service` does, and
`.github/scripts/deploy_koyeb.py` is written and dry-run-verified against both
live service definitions, ready to switch on.

It is blocked on credentials. The reused `GHCR_PAT` is **fine-grained and
scoped to the `draftproof-api` package**, so it can neither create nor read
`expressautomate-api`. Pushing as `GITHUB_TOKEN` instead produces a package
only that ephemeral token can read, which Koyeb cannot pull.

Two ways to unblock, both yours to do:

**A — new token (simplest).** GitHub → Settings → Developer settings →
Personal access tokens → a **classic** token with `write:packages` and
`read:packages`. Then:

```bash
gh secret set GHCR_PAT --body '<new token>' --repo kianwoon/expressautomate
```

**B — keep the existing fine-grained PAT.** Let one build push as
`GITHUB_TOKEN` to create the package, then GitHub → your packages →
`expressautomate-api` → Package settings → add the PAT's scope to it.

Either way, refresh the Koyeb registry secret so Koyeb can pull:

```bash
printf '%s' '<token>' | koyeb secrets update ghcr-expressautomate \
  --type registry-private --registry-url ghcr.io \
  --registry-username kianwoon --value-from-stdin
```

Then in `.github/workflows/ci.yml`, restore the `build` job (docker/login with
`GHCR_PAT`, `build-push-action` with `cache-from/to: type=gha`), make `deploy`
depend on `[test, build]`, and replace the Koyeb CLI steps with
`python3 .github/scripts/deploy_koyeb.py`. That script's payload transform is
covered by `backend/tests/test_deploy_payload.py`.

### Deployment path

CI deploys with `koyeb deploy` (archive upload, Koyeb builds the Dockerfile)
rather than pushing to a registry. The Actions build *can* push to GHCR, but
the resulting package is private and readable only by the Actions-scoped
`GITHUB_TOKEN`, which Koyeb cannot reuse — archive deploy keeps `KOYEB_TOKEN`
as the single deployment credential instead of minting a second long-lived PAT.

### DNS

| Record | Status |
|---|---|
| `expressautomate.app` → `…cname.koyeb.app` | **live**, TLS valid |
| `www.expressautomate.app` | **not resolving** |

`www` is created in Cloudflare and ACTIVE on Koyeb, but Cloudflare's
authoritative nameservers return no answer for it while serving the apex
normally. The record is identical in shape to the working apex, so this looks
like zone settling on a domain registered the same day rather than a
misconfiguration. If it has not resolved within 24 hours, the cleaner fix is
to drop the `www` CNAME entirely and add a Cloudflare Redirect Rule sending
`www` → apex, which is the usual arrangement anyway.

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
stored OAuth refresh tokens per §30).

There is deliberately **no shared webhook secret**. Each Graph subscription
generates its own random `clientState` when it is created and stores it on the
row; the webhook compares incoming notifications against that. One shared value
would make every tenant's notifications forgeable the moment it leaked
anywhere — per-subscription limits that to a single mailbox.

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
2. Name `expressautomate.app`; supported accounts: **Accounts in any organizational directory and personal Microsoft accounts** (`AzureADandPersonalMicrosoftAccount`, with `api.requestedAccessTokenVersion: 2`). Work/school sign-ins make each agency a tenant; personal accounts (outlook.com, hotmail.com, live.com) all report one shared MSA tenant GUID, so each of those users gets a private tenant derived from their own `oid` — see `_tenant_for` in `backend/app/api/auth.py`. `MS_TENANT_ID=common` accordingly.
3. Redirect URIs (Web) — **two** are needed, because consent is incremental:
   - `http://localhost:8000/api/auth/microsoft/callback` — sign-in
     (every route lives under `/api`; match what `MS_REDIRECT_URI` in `.env` says)
   - `http://localhost:8000/api/mailboxes/connect/callback` — mailbox consent

   Entra rejects any redirect it has not been told about, and the failure shows
   up on the consent screen rather than in the logs.
4. **Certificates & secrets** → new client secret → copy the *Value*
5. **API permissions** → Microsoft Graph → *Delegated* → add exactly:
   `openid`, `profile`, `email`, `User.Read`, `Mail.Read`, `offline_access`

   Do **not** add `Mail.ReadWrite`, `Mail.Send`, or any Application permission —
   read-only access is a product promise (§6.1). `Mail.Read` is also the
   least-privileged permission Graph accepts for message change notification
   subscriptions, so nothing broader is needed for ingestion.

   Do **not** grant admin consent on the tenant's behalf. Each user consents to
   mailbox access when they connect their own mailbox.

Then fill in the repo-root `.env`:

```
MS_CLIENT_ID=<Application (client) ID>
MS_CLIENT_SECRET=<secret Value>
MS_IDENTITY_SCOPES=openid profile email User.Read offline_access
MS_MAILBOX_SCOPES=Mail.Read
MS_MAILBOX_REDIRECT_URI=<the second redirect URI above>
```

**The scopes are two keys, not one.** Signing in requests `MS_IDENTITY_SCOPES`
only; `MS_MAILBOX_SCOPES` is requested separately when a user connects a
mailbox, so nobody is asked to hand over their mail before they have asked for
mail ingestion. Entra's consent is cumulative per user and app, so the token
stored after the second grant covers both.

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

## What RLS does and does not protect

Row-level security guards against **application bugs** — a missing `WHERE
tenant_id = ...`, a mis-scoped join, a handler that forgets to set the tenant.
Those are the realistic failure modes and RLS makes them return zero rows
instead of another agency's data.

It does **not** protect against **credential theft**. Anyone holding the
`expressautomate_app` password can call `set_config('app.tenant_id', <any
uuid>)` and read every tenant. The controls that matter there are secret
management, network exposure, and rotation — not the policy. Treat
`DATABASE_URL` with the same care as `DATABASE_ADMIN_URL`.

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
