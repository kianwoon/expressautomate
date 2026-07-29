#!/usr/bin/env bash
# Run the backend suite the way CI runs it.
#
# CI has no `.env` — every value below is an environment variable, and that is
# load-bearing rather than incidental. A developer's `.env` is pointed at the
# production database and carries the production application-role password;
# `20260726_1800_row_level_security.py` ALTERs that role's password to whatever
# `DATABASE_APP_PASSWORD` says, so a suite run that inherits the real `.env`
# rewrites the local role's password partway through and every connection
# opened afterwards fails to authenticate. The symptom is a run that fails a
# different number of tests each time, which reads like flakiness and is not.
#
# So: this script hides any `.env` from pydantic for the duration of the run,
# and supplies the same variables CI does. Values mirror
# `.github/workflows/` — keep them in step.
#
# Usage, from `backend/`:
#   scripts/test-env.sh                    # whole suite
#   scripts/test-env.sh tests/test_cv_text.py -q
#
# If the suite starts failing in bulk, the database has accumulated rows from
# an interrupted run — `rescan_stuck` counting 140 stranded rows where a test
# expects 0 is the giveaway, and it looks like an authentication problem
# because the failures are loud and numerous. Rebuild rather than debug:
#
#   docker rm -f ea-test-db
#   docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres \
#     -e POSTGRES_DB=expressautomate --name ea-test-db postgres:16
#   sleep 12
#   docker exec ea-test-db psql -U postgres -d expressautomate \
#     -c "CREATE ROLE expressautomate_app LOGIN PASSWORD 'test-app-password' NOBYPASSRLS;"
#   scripts/test-env.sh
#
# The password above must match `.env.test`. CI uses a different one, and
# copying CI's value here is the wrong move — the local role is provisioned
# from `.env.test`, so the two disagree and every connection fails to
# authenticate.
#
# allow-hardcode: these are CI's own inert test values, copied deliberately so
# a local run and a CI run agree. They are not configuration for anything that
# runs for real.
set -euo pipefail

PORT="${EA_TEST_DB_PORT:-5433}"
HOST="127.0.0.1"

# `backend/.env.test` is the repo's own local test configuration, and it is
# the source of truth here rather than a copy of CI's values. It already
# points at the throwaway container on 5433 and names the application-role
# password the local role is provisioned with. Reproducing CI's values instead
# is what caused a long detour: the two disagree on that password, so forcing
# CI's over the local role made every connection fail authentication.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
# shellcheck disable=SC1091
. "$HERE/.env.test"
set +a

# The one thing the environment variables above cannot do on their own. Any
# `.env` in the repo root or the worktree root is read by pydantic-settings and
# would reintroduce exactly the drift this script exists to prevent, so it is
# hidden for the length of the run and restored on the way out however the run
# ends.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HIDDEN=""
if [ -e "$ROOT/.env" ]; then
  HIDDEN="$ROOT/.env.hidden-while-testing"
  mv "$ROOT/.env" "$HIDDEN"
  # INT and TERM as well as EXIT: a bare EXIT trap does not run when the shell
  # is killed by a signal, and "killed" includes the ordinary case of somebody
  # pressing Ctrl-C through a slow suite. Leaving `.env` renamed is the failure
  # this trap exists to prevent, so it has to survive the ways a run really
  # ends rather than only the tidy one.
  trap 'mv "$HIDDEN" "$ROOT/.env"' EXIT INT TERM
fi

# Bring the schema and the application role up to head before every run.
#
# CI gets a brand-new Postgres container per job, so it never notices that a
# completed suite leaves the local database in a state the next run cannot
# authenticate against: the run ends with the application role's password no
# longer matching `DATABASE_URL`, and the following run fails several hundred
# tests with `InvalidPasswordError` on every connection opened after the pool
# warms. Re-running the migration is what repairs it, because
# `20260726_1800_row_level_security.py` re-issues `ALTER ROLE ... PASSWORD`
# from `DATABASE_APP_PASSWORD`, which this script pins.
#
# Doing it here rather than hunting the mutator is deliberate: this makes a
# local run start from the same known state a CI run does, which is the
# property that actually matters. It is idempotent and costs a second.
uv run alembic upgrade head >/dev/null

# Not `exec`. `exec` replaces this shell with pytest, and a process that has
# been replaced never runs its EXIT trap — so the `.env` hidden above was
# restored on no run at all, not merely on an interrupted one. It sat as
# `.env.hidden-while-testing` until somebody noticed, which meant the file
# holding every production secret was living under a name that no ignore rule
# matched, one `git add -A` from being committed.
#
# Without `exec` the trap fires and `set -e` still carries pytest's exit code
# out of the script, so a failing suite still fails the caller.
uv run pytest "$@"
