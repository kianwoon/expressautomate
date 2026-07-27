# expressautomate.app

AI recruitment intelligence & operations platform. Governing spec:
[expressautomate_app_high_level_implementation_plan.md](expressautomate_app_high_level_implementation_plan.md)
— section numbers (§) in code comments refer to it.

**Vertical: recruitment.** Confirmed 2026-07-27. Small Singapore recruitment
agencies on Microsoft 365, 3–50 recruiters. The landing page still carries
generic horizontal-SMB copy and needs rewriting against this vertical.

## Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js static export + nginx (`frontend/`) |
| Backend | FastAPI / Python 3.12, `uv` (`backend/`) |
| DB | Postgres 16 on Koyeb — `expressautomate` database on the `draftproof-db` instance |
| Migrations | Alembic, async engine |
| AI | OpenRouter (fast model default, strong model on low confidence — §32) |
| Hosting | Koyeb; DNS via Cloudflare |

## Commands

Run from `backend/`:

```bash
uv sync                                     # install
uv run uvicorn app.main:app --reload        # dev server on :8000
uv run alembic revision --autogenerate -m "msg"
uv run alembic upgrade head
uv run pytest
uv run ruff check .
```

## Conventions

- **All config comes from the repo-root `.env`** via `app.core.config.settings`.
  Nothing hardcoded — no literal URLs, model names, or keys in source.
- **`sslmode` is stripped from the DSN** for asyncpg and re-expressed as an SSL
  context in `settings.asyncpg_connect_args`. Koyeb's cert is not in the system
  trust store, so `require` maps to encrypt-without-verify.
- **Every business table carries `tenant_id`** (§18) via the `TenantScoped`
  mixin. Agency A must never read Agency B's data.
- **Preserve source provenance while the source is legitimately retained**
  (amends §2.3, decided 2026-07-27). Raw email and AI-derived data live in
  separate tables; extractions keep evidence, confidence, and model version.
  Retention is configurable per tenant rather than indefinite — see
  [the ingestion design](docs/superpowers/specs/2026-07-27-email-ingestion-design.md).
- **The AI must not fabricate missing values** (§15) — return `Not mentioned`.

## Auth

Users sign in with **Microsoft or Google** (decided 2026-07-27; amends plan
§6.1, which assumed Microsoft only). Keep the two concerns separate:

- *Identity* — either provider.
- *Mailbox ingestion* — Microsoft Graph only. There is no Gmail path.

A Google-only user therefore has nothing to ingest. Onboarding must say so
rather than showing an empty dashboard.

**Both work/school and personal Microsoft accounts sign in** (decided
2026-07-27; `MS_TENANT_ID=common`). Every personal account reports the same
MSA tenant GUID, so it is never used as a tenant key: `_tenant_for` in
`app/api/auth.py` gives each such user a private tenant derived as a UUIDv5
over their `oid`, flagged `tenants.is_personal_account`. Work accounts still
key on `tid`, so colleagues share one agency tenant.

## Deploy

`backend/**` and `frontend/**` deploy independently — see `.github/workflows/`.
Tests run against a throwaway Postgres container, never the live database:
they create roles and toggle RLS, and `tests/conftest.py` refuses to run
against a non-local host.

**One Koyeb service serves everything.** `api` owns route `/`: FastAPI serves
the Next.js static export (built into the image) at `/`, and every API route
lives under an `/api` router. Nothing strips a prefix, so `API_ROOT_PATH` is
empty. `tests/test_routing.py` fails if a route escapes `/api`, where the
static mount would shadow it.

Two Koyeb settings are **not** in this repo and must be re-applied by hand if
the service is ever recreated — both caused outages when they drifted:

| Setting | Value |
|---|---|
| Route | `/` (not `/api`) |
| Health check | `/api/health` (not `/health` — a 404 there leaves the deploy `PENDING` until CI times out) |

**Per-service env vars are also not in this repo.** The workflow deploys the
image; the variables were set by hand, so they drift per service. `GRAPH_BASE_URL`
was missing on `api` for a day — harmless until the inbox preview became the
first web-service code to call Graph, then every preview 500ed. Anything that
adds a Graph call to a service that had none needs its env checked:

```bash
koyeb deployment get $(koyeb deployments list --service <id> -o json | jq -r '.deployments[0].id') -o json | jq -r '.deployment.definition.env[].key'
```

Live: https://expressautomate.app · repo: `kianwoon/expressautomate` (private)

## Status

**Stage 1 (Foundation) done and deployed:** env, database, tenant/user model,
migrations, RLS, logging, health endpoints, worker skeleton, CI/CD.

**Blocked** on credentials only you can create — Entra ID app registration and
Google OAuth client. See [docs/setup.md](docs/setup.md).

Not started: frontend (`frontend/` is empty), Microsoft OAuth flow, ingestion,
AI extraction.
