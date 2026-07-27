"""flush_notification_deliveries

`notification_deliveries` carries `FORCE ROW LEVEL SECURITY` (the
`notifications` migration), so a raw `UPDATE` from the unscoped supervisor
process — no single tenant context to set, like every other sweep in
`app/workers/tasks.py` — sees and touches nothing: the `tenant_isolation`
policy filters it down to zero rows before the `WHERE` clause ever runs, and
silently, since RLS is a filter, not an error. Like `stalled_email_rows` and
`claim_fetched_email_rows`, this goes through a narrow `SECURITY DEFINER`
function instead of a role that bypasses RLS.

This one writes, for the same reason `claim_fetched_email_rows` does: the
promotion to `pending` has to happen in the same statement that selects the
rows, or the next tick claims the same batch again.

Three cases share the one function: `pending` past the stale window (a lost
`enqueue`), `suppressed` past the cap window (the rollup's tail — no later
delivery arrived to carry its "+N more"), and `sending` past the stale window
(a `deliver_notification` claim whose worker was killed outright — SIGKILL,
OOM, container eviction — before any of its exception handlers ran, so
nothing else ever moved the row back to `pending`; arq's retry itself
requires `status = 'pending'` to reclaim). The `sending` case is gated on
`updated_at`, which the `touch_updated_at` trigger (also bound on this table)
stamps on the claim's own `UPDATE` — gating it on `created_at` instead would
measure time-since-the-row-was-created rather than time-since-claimed, and
could repromote a row a live worker had only just claimed.

Revision ID: b1a1000a061c
Revises: 9cb3950eccff
Create Date: 2026-07-28
"""

from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "b1a1000a061c"
down_revision: str | None = "9cb3950eccff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "flush_notification_deliveries(p_stale_minutes int, p_limit int)"
ARG_TYPES = "int, int"
BODY = """
RETURNS TABLE (id uuid, tenant_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- This is SECURITY DEFINER and runs with RLS bypassed, so it must not
    -- trust its own arguments just because today's only caller (the
    -- supervisor in app/workers/tasks.py) passes fixed, sane values. EXECUTE
    -- is granted to the application role, which is the same role a web
    -- request handler runs as — a future handler that forwards a caller-
    -- supplied value here must not be able to turn it into an incident.
    --
    -- Floor the stale window at 1 minute: `p_stale_minutes => 0` (or a
    -- negative value) would make every 'sending' row stale immediately,
    -- including one a live worker claimed microseconds ago, and arq would
    -- then redeliver it — a duplicate message to a recruiter. One minute is
    -- comfortably longer than a single claim-to-ack round trip.
    --
    -- Cap the limit at 500: the worker's real batches are far smaller, but
    -- an unbounded LIMIT would let one call UPDATE the entire table under a
    -- single FOR UPDATE SKIP LOCKED lock, holding it for as long as that
    -- takes. 500 rows is enough headroom for a large backlog to drain in a
    -- few ticks without turning one call into a table-wide lock.
    UPDATE notification_deliveries d
    SET status = 'pending'
    WHERE d.id IN (
        SELECT c.id FROM notification_deliveries c
        WHERE (c.status = 'pending'
               AND c.created_at < now() - make_interval(mins => GREATEST(p_stale_minutes, 1)))
           OR (c.status = 'suppressed'
               AND c.created_at < now() - interval '1 hour')
           OR (c.status = 'sending'
               AND c.updated_at < now() - make_interval(mins => GREATEST(p_stale_minutes, 1)))
        ORDER BY c.created_at
        LIMIT LEAST(GREATEST(p_limit, 1), 500)
        FOR UPDATE SKIP LOCKED
    )
    RETURNING d.id, d.tenant_id
$$
"""


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {BODY}")
    # Postgres grants EXECUTE to PUBLIC by default, which would hand an
    # RLS-bypassing function to every role that can connect.
    op.execute(
        f"REVOKE ALL ON FUNCTION flush_notification_deliveries({ARG_TYPES}) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' flush_notification_deliveries({ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS flush_notification_deliveries({ARG_TYPES})")
