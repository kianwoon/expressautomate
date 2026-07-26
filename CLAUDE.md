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
| Frontend | Next.js / React / TypeScript (`frontend/`, not yet scaffolded) |
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
- **Never discard the source email** (§2.3). Raw email and AI-derived data live
  in separate tables; extractions keep evidence, confidence, and model version.
- **The AI must not fabricate missing values** (§15) — return `Not mentioned`.

## Auth

Users sign in with **Microsoft or Google** (decided 2026-07-27; amends plan
§6.1, which assumed Microsoft only). Keep the two concerns separate:

- *Identity* — either provider.
- *Mailbox ingestion* — Microsoft Graph only. There is no Gmail path.

A Google-only user therefore has nothing to ingest. Onboarding must say so
rather than showing an empty dashboard.

## Deploy

Push to `main` → GitHub Actions lints, migrates, tests, then deploys both
Koyeb services. Tests run against a throwaway Postgres container, never the
live database — they create roles and toggle RLS.

Live: https://expressautomate.app · repo: `kianwoon/expressautomate` (private)

## Status

**Stage 1 (Foundation) done and deployed:** env, database, tenant/user model,
migrations, RLS, logging, health endpoints, worker skeleton, CI/CD.

**Blocked** on credentials only you can create — Entra ID app registration and
Google OAuth client. See [docs/setup.md](docs/setup.md).

Not started: frontend (`frontend/` is empty), Microsoft OAuth flow, ingestion,
AI extraction.
