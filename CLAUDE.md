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

**The `gateway` service is the exception to "one image".** The WA gateway
(`gateway/`, Node 22 + Fastify — see
[the plan](docs/superpowers/specs/2026-07-29-baileys-gateway-plan.md)) runs its
own image, `ghcr.io/kianwoon/expressautomate-gateway`, tagged from a hash of
`gateway/` alone. Its hand-set Koyeb settings, recorded here for the same
reason as the two above:

| Setting (service `gateway`) | Value |
|---|---|
| Route | **none** — no public route at all. It is called only by `api` over the private mesh; the browser never reaches it. |
| Port | `7300:tcp` — **`tcp`, not `http`, and that is what makes it private.** Creating the service with `--ports 7300:http` silently added a public route `/` on it, which `api` already owns in this app. Removing the route alone does not stick: a http port keeps its route. The two changes have to travel together — `--port 7300:tcp --route '!/'`. |
| Health check | `GET /health` on port **7300** (`WA_GATEWAY_PORT`). Unauthenticated by design — an authenticated health check fails the same way a 404 does. |
| Scale | **pinned to 1, autoscaling off, forever.** A Baileys socket is process-local; two instances fight over the same WhatsApp session and get both logged out. |

**Created 2026-07-29**, service id `50680cdb`, region `was`, `eco-nano`. The
shared secret lives once, as the Koyeb secret `ea-wa-gateway-shared-secret`,
and both services reference it rather than each holding a copy — two copies
are two things that can drift, which is what this whole section is about. It
is reached at **`http://gateway.expressautomate.internal:7300`**.

**The `.internal` suffix is load-bearing, and Koyeb's own documentation will
tell you otherwise.** The prose on their service-mesh page gives the format as
`<service>.<app>`; the table further down the same page gives
`<service>.<app>.internal`, and the table is the one that resolves. Every web
search surfaces the prose. Half a day went into this: `gateway.expressautomate`
and `gateway.expressautomate.koyeb` both fail with a bare `ConnectError`, which
looks exactly like a firewall or a dead service rather than a name that does
not exist.

What proves it is a naming problem rather than a connectivity one: a pod IP
works. If mesh calls are failing, put the target's pod IP in the URL — if that
succeeds, the network is fine and only the name is wrong. Do not leave it
there: a pod IP rotates on every deploy, including the deploy your own env
change triggers, so the address goes stale the moment you set it.

The bare `<service>` form (`http://gateway:7300`) is documented to work across
regions and is untested here.

Its env vars are set by hand and start empty: `WA_GATEWAY_SHARED_SECRET`
(required — the process refuses to boot without it) and, from P2, the database
URL and `WA_GATEWAY_ENCRYPTION_KEY`. `api` needs `WA_GATEWAY_URL` +
`WA_GATEWAY_SHARED_SECRET` once it first calls the gateway (P3) — the same
"first call to an external system" rule as `GRAPH_BASE_URL` and `R2_*` below.

**Per-service env vars are also not in this repo.** The workflow deploys the
image; the variables were set by hand, so they drift per service. `GRAPH_BASE_URL`
was missing on `api` for a day — harmless until the inbox preview became the
first web-service code to call Graph, then every preview 500ed. It happened a
second time with `R2_*`: only `worker` had the five keys, so candidate avatar
upload — the first web-service code to touch R2 — died on
`ValueError: Invalid endpoint:` with an empty `R2_ENDPOINT_URL`.

The pattern is not about Graph. **Whenever a service gains its first call to
any external system, check that service's env before shipping** — the tests
pass either way, because nothing in this repo knows what Koyeb was told:

```bash
koyeb deployment get $(koyeb deployments list --service <id> -o json | jq -r '.deployments[0].id') -o json | jq -r '.deployment.definition.env[].key'
```

Live: https://expressautomate.app · repo: `kianwoon/expressautomate` (private)

## Status

**Stage 1 (Foundation) done and deployed:** env, database, tenant/user model,
migrations, RLS, logging, health endpoints, worker skeleton, CI/CD.

**Shipped since:** OAuth sign-in, mailbox ingestion, AI extraction, clients,
candidates, sourcing, the WhatsApp gateway, job order assignment and
sharing (2026-07-31 — see
[the design](docs/superpowers/specs/2026-07-30-job-order-assignment-and-sharing-design.md)),
and candidate ownership and sharing (2026-07-31 — see
[the design](docs/superpowers/specs/2026-07-31-candidate-ownership-and-sharing-design.md)).

**Candidates are private to their owning recruiter**, shared read-only to a
named colleague or the whole agency. Two rules follow from it that are easy to
break by accident:

- **Per-tenant email/phone uniqueness is deliberately kept**, so one person is
  one row. A recruiter who enters someone a colleague privately holds gets a
  409 disclosing an abbreviated, contact-masked name and the holder — nothing
  else — plus a request-access path. `masked_candidate()` in
  `app/services/candidate_matching.py` is the single implementation of that
  boundary; the collision path and redacted sourcing matches both call it.
**There is an unfinished rollout step, and it is the kind that gets forgotten.**
Migration `c1a0d5e7b206` gave every candidate existing at deploy time a
`scope='tenant'` share with `shared_by_user_id = NULL`, so the database stayed
exactly as visible as it was and nobody lost sight of anything mid-week. **The
feature's actual privacy does not take effect until those rows are deleted.**
Once the agency has been told, run this as the migration/superuser role — the
app role is under `FORCE ROW LEVEL SECURITY` and would only delete its own
tenant:

```sql
DELETE FROM candidate_shares WHERE scope = 'tenant' AND shared_by_user_id IS NULL;
```

To see whether it is still outstanding:

```sql
SELECT count(*) FROM candidate_shares WHERE scope = 'tenant' AND shared_by_user_id IS NULL;
```

A deliberate broadcast whose author was later deleted also matches that
predicate (the FK is `SET NULL`) and would be swept up. It is a read grant any
recruiter can simply re-make, which is why the residual was accepted rather
than designed around.

- **Match tenant-wide, disclose at the edge.** Import matching and sourcing
  both scan the whole agency on purpose — a visibility-filtered lookup would
  miss an invisible row and then die on the unique index. Redaction happens at
  read, per viewer. `tests/test_candidate_routes_guarded.py` enforces both
  halves: every by-id read goes through a guard, and no module outside the
  named exemptions may `select(Candidate)` at all.

**`frontend/` is not empty** — Next.js static export with `/dashboard` (job
orders), `/dashboard/clients`, `/dashboard/candidates` and `/settings`. Plain
React hooks, no state library, no component library, hand-rolled CSS with tokens
in `app/globals.css`. Tests are Vitest + Testing Library, colocated. Its only
runtime dependencies are `next`, `react` and `qrcode`; keep it that way unless
there is a reason not to.
