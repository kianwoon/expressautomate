#!/usr/bin/env bash
# Provision a throwaway Postgres for the gateway's store tests, then run them.
#
# The store tests are worth nothing against a fake: `ON CONFLICT` semantics, the
# bytea round trip, and RLS via `set_config('app.tenant_id', …)` are all
# Postgres behaviours, and a hand-rolled double would agree with whatever bug
# the store has. So this points at the same throwaway container the backend
# suite uses (`ea-test-db` on 5433) but creates its **own database** — sharing
# `expressautomate` would let a gateway run and a backend run corrupt each
# other's rows.
#
# The schema comes from the real Alembic migrations, not a hand-written CREATE
# TABLE, so a drift between the migration and what the gateway expects fails
# here rather than on Koyeb.
#
# Usage, from `gateway/`:  scripts/test-db.sh
#
# allow-hardcode: inert local test values, mirroring backend/.env.test so a
# gateway run and a backend run agree about the container. Nothing here
# configures anything that runs for real.
set -euo pipefail

PORT="${EA_TEST_DB_PORT:-5433}"
HOST="127.0.0.1"
DB="${WA_GATEWAY_TEST_DB:-wa_gateway_test}"
CONTAINER="${EA_TEST_DB_CONTAINER:-ea-test-db}"

docker exec "$CONTAINER" psql -U postgres -d postgres -tc \
  "SELECT 1 FROM pg_database WHERE datname = '$DB'" | grep -q 1 ||
  docker exec "$CONTAINER" psql -U postgres -d postgres -c "CREATE DATABASE $DB"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$(cd "$HERE/../backend" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# The repo-root .env points at the live database and pydantic-settings reads it
# regardless of the exported variables' precedence for *other* keys; hide it,
# exactly as backend/scripts/test-env.sh does and for the same reason.
HIDDEN=""
if [ -e "$ROOT/.env" ]; then
  HIDDEN="$ROOT/.env.hidden-while-testing"
  mv "$ROOT/.env" "$HIDDEN"
  trap 'mv "$HIDDEN" "$ROOT/.env"' EXIT
fi

set -a
# shellcheck disable=SC1091
. "$BACKEND/.env.test"
DATABASE_ADMIN_URL="postgresql://postgres:postgres@$HOST:$PORT/$DB"
DATABASE_URL="postgresql://expressautomate_app:test-app-password@$HOST:$PORT/$DB"
set +a

(cd "$BACKEND" && uv run alembic upgrade head >/dev/null)

# The gateway connects as the same restricted, NOBYPASSRLS role the API uses,
# so the store tests prove the policy admits the gateway's own writes rather
# than silently running as a superuser that ignores RLS entirely.
export WA_GATEWAY_TEST_DATABASE_URL="$DATABASE_URL"
# The negative case for the boot-time assertion in db.ts: `postgres` here is
# the container superuser (`rolbypassrls = true`), which is exactly the role
# `assertRlsNotBypassed` exists to refuse. Never used to open a real pool
# except inside that one test.
export WA_GATEWAY_TEST_BYPASS_DATABASE_URL="$DATABASE_ADMIN_URL"
cd "$HERE"
exec npm test "$@"
