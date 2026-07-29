"""sweep_stale_wa_sends

P5's liveness sweep half: a `pending` `candidate_activities` row (P5's
pre-dispatch write — `20260729_2300_wa_send_pending_and_spacing.py`) older
than a sensible bound means the process that dispatched it died before it
could resolve the row to `sent`/`failed`/`unknown` — nobody will ever learn
what WhatsApp did with it. It becomes `unknown`, and only `unknown`: §15
forbids `failed` here exactly as it forbids it in `app/services/wa_gateway.py`
— we never observed a refusal, only silence.

Same reasoning as `flush_notification_deliveries` for why this is a
`SECURITY DEFINER` SQL function rather than a plain `UPDATE` from
`app/workers/tasks.py`: `candidate_activities` carries `FORCE ROW LEVEL
SECURITY` and the supervisor process sets no `app.tenant_id`, so an unscoped
`UPDATE` would match zero rows, silently.

The `UPDATE ... WHERE status = 'pending'` clause is the compare-and-set the
P5 review asked for: it can only ever move a `pending` row to `unknown`, never
touch a `sent` or `failed` row that resolved in the gap between this
function's SELECT and its UPDATE (there is no such gap — one statement), and
never re-touch a row this same sweep already moved on a previous tick.

Revision ID: d8e51f0a2b6c
Revises: c3f8a2d5e194
Create Date: 2026-07-29 23:30:00+00:00
"""
from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "d8e51f0a2b6c"
down_revision: str | None = "c3f8a2d5e194"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "sweep_stale_wa_sends(p_stale_minutes int, p_limit int)"
ARG_TYPES = "int, int"
BODY = """
RETURNS TABLE (id uuid, tenant_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- SECURITY DEFINER, RLS bypassed — same "do not trust your own
    -- arguments" reasoning as flush_notification_deliveries: EXECUTE is
    -- granted to the application role, the same role a web request runs as.
    --
    -- Floor at 1 minute, same reason as flush_notification_deliveries: a
    -- p_stale_minutes of 0 or less would sweep a send that dispatched a
    -- moment ago, while the request handling it is still very much alive.
    -- The real default (WA_SEND_STALE_PENDING_MINUTES, 10) is what actually
    -- keeps this comfortably above the gateway's own timeout.
    --
    -- Cap at 500 for the same reason as flush_notification_deliveries: bound
    -- how long one call can hold FOR UPDATE SKIP LOCKED.
    WITH locked AS (
        SELECT id FROM candidate_activities
        WHERE status = 'pending'
          AND created_at < now() - make_interval(mins => GREATEST(p_stale_minutes, 1))
        ORDER BY created_at
        LIMIT LEAST(GREATEST(p_limit, 1), 500)
        FOR UPDATE SKIP LOCKED
    )
    UPDATE candidate_activities a
    SET status = 'unknown',
        error = 'The message was sent to WhatsApp but the process handling '
                'it did not come back before we gave up waiting, so we do '
                'not know whether it went out.'
    FROM locked l
    WHERE a.id = l.id
    RETURNING a.id, a.tenant_id
$$
"""


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {BODY}")
    op.execute(f"REVOKE ALL ON FUNCTION sweep_stale_wa_sends({ARG_TYPES}) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' sweep_stale_wa_sends({ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS sweep_stale_wa_sends({ARG_TYPES})")
