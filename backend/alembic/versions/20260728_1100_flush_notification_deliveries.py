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
    -- SKIP LOCKED so two supervisors split a backlog instead of one blocking
    -- on the other's claim. ORDER BY created_at drains oldest-first.
    UPDATE notification_deliveries d
    SET status = 'pending'
    WHERE d.id IN (
        SELECT c.id FROM notification_deliveries c
        WHERE (c.status = 'pending'
               AND c.created_at < now() - make_interval(mins => p_stale_minutes))
           OR (c.status = 'suppressed'
               AND c.created_at < now() - interval '1 hour')
           OR (c.status = 'sending'
               AND c.updated_at < now() - make_interval(mins => p_stale_minutes))
        ORDER BY c.created_at
        LIMIT p_limit
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
