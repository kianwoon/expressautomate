"""sweep_stale_client_discovery

A client-discovery run is a user-facing button, not pipeline state: nothing
swept it, so a run that could never be claimed (an enqueue lost after commit)
or whose worker died mid-scan (a `running` row nobody will resume) sat there
until the recruiter happened to scan again. This adds the supervisor sweep
that parks such rows in `failed` with words the recruiter can act on.

Two kinds of row, one function:

- `pending` past the stale window: the enqueue was lost — `enqueue` fails
  soft after the transaction committed, so the row is durable and the job is
  not, and arq's own retry only ever resumes `pending`/`running` rows that a
  job actually claimed. Nothing else looks at a `pending` row, so without
  this branch the row is lost silently and permanently.
- `running` past the stale window: the worker that claimed it was killed
  outright (SIGKILL, OOM, container eviction) before any of its exception
  handlers ran. arq's in-flight retry covers a worker killed mid-job (the
  claim accepts `running` again), but only while arq still has the job
  scheduled; a worker that dies after arq gave up leaves the row `running`
  forever. The design's next-scan-supersedes path only fires when the
  recruiter clicks again — this branch makes the failure visible on its own.

`failed` is the right terminal state for both, unlike the WA send sweep's
`unknown`: a discovery run has no externally-observable half (no WhatsApp
message that may or may not have gone out) — an abandoned run simply did not
finish, and "scan again" is the truthful, actionable answer.

Same reasoning as `sweep_stale_wa_sends` for why this is a `SECURITY DEFINER`
SQL function rather than a plain `UPDATE` from `app/workers/tasks.py`:
`client_discovery_runs` carries `FORCE ROW LEVEL SECURITY` and the supervisor
process sets no `app.tenant_id`, so an unscoped `UPDATE` would match zero
rows, silently.

The `WHERE status = ...` clause is the compare-and-set that makes the sweep
safe to race: it can only move a row *still in the state being swept* — never
a row that just started (fresh `created_at`/`updated_at`), never one the job
just claimed (the claim UPDATE touches `updated_at`), and never one already
settled. The `updated_at` clock is what keeps a genuinely-slow scan from
being swept out from under a live worker, exactly as `flush_notification_deliveries`
gates its `sending` branch.

Revision ID: c1v2j0000001
Revises: c1v2i0000001
Create Date: 2026-08-12 10:00:00+00:00
"""
from collections.abc import Sequence

from alembic import op
from app.core.config import settings

revision: str = "c1v2j0000001"
down_revision: str | None = "c1v2i0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "sweep_stale_client_discovery_runs(p_stale_minutes int, p_limit int)"
ARG_TYPES = "int, int"
BODY = """
RETURNS TABLE (id uuid, tenant_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- SECURITY DEFINER, RLS bypassed — same "do not trust your own
    -- arguments" reasoning as sweep_stale_wa_sends: EXECUTE is granted to the
    -- application role, the same role a web request runs as.
    --
    -- Floor at 1 minute, same reason as sweep_stale_wa_sends: a stale window
    -- of 0 or less would sweep a run a worker just claimed, while it is still
    -- very much alive. The real defaults (CLIENT_DISCOVERY_STALE_PENDING_MINUTES
    -- and CLIENT_DISCOVERY_STALE_RUNNING_MINUTES) sit well above both the job
    -- timeout and the scan POST's own stale check.
    --
    -- Cap at 500 for the same reason as the other sweeps: bound how long one
    -- call can hold FOR UPDATE SKIP LOCKED.
    --
    -- `updated_at` gates the `running` branch (time since the claim wrote it),
    -- `created_at` the `pending` branch (time since the row was inserted) —
    -- the same split flush_notification_deliveries makes, for the same reason:
    -- a row can sit `pending` for most of the window before a worker claims
    -- it, and gating that branch on the claim write would be measuring the
    -- wrong clock.
    WITH locked AS (
        SELECT id FROM client_discovery_runs
        WHERE (
            (status = 'pending' AND created_at < now()
                - make_interval(mins => GREATEST(p_stale_minutes, 1)))
            OR (status = 'running' AND updated_at < now()
                - make_interval(mins => GREATEST(p_stale_minutes, 1)))
        )
        ORDER BY created_at
        LIMIT LEAST(GREATEST(p_limit, 1), 500)
        FOR UPDATE SKIP LOCKED
    )
    UPDATE client_discovery_runs r
    SET status = 'failed',
        finished_at = now(),
        error = CASE WHEN r.status = 'pending'
                     THEN 'The scan could not be queued. Scan again in a minute.'
                     ELSE 'The scan stopped before it finished. Scan again to retry.'
                END
    FROM locked l
    WHERE r.id = l.id
    RETURNING r.id, r.tenant_id
$$
"""


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {BODY}")
    op.execute(f"REVOKE ALL ON FUNCTION sweep_stale_client_discovery_runs({ARG_TYPES}) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' sweep_stale_client_discovery_runs({ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS sweep_stale_client_discovery_runs({ARG_TYPES})")
