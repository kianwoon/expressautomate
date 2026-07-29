"""wa_liveness_sweep

Plan §6's liveness sweep, the other half: `sweep_stale_wa_sends`
(`20260729_2330_sweep_stale_wa_sends.py`) resolves stale *sends*; this
resolves stale *knowledge about a session*. Per §6, the gateway is the only
writer of `wa_sessions.status` (`POST /api/wa/internal/status`), and it
already pushes that callback whenever a session's status changes — so a
background sweep does not need to write status itself. It only needs to ask
the gateway "are you still there?" for sessions that claim to be live, often
enough that a socket dying silently is noticed within a bounded time. Asking
is a call to `WaGatewayClient.status`, made from `app.workers.tasks
.sweep_wa_liveness`, not SQL — there is nothing for a database function to do
here except claim rows, which is what `wa_sweep_claim_due_sessions` below is
for.

**Naming decision**, per the same reviewer who asked for a resolution rather
than two meanings sharing one name: `GET /api/wa/session` already returns a
JSON field called `last_checked_at`, computed as `datetime.now()` at request
time (`app/api/wa_gateway.py#_call_gateway`) — "when this browser request
looked", never a database read. This sweep's column means something
different: "when a *background* check last ran". Reusing the same word for
both would be exactly the trap CLAUDE.md warns about, so the column here is
named **`last_liveness_check_at`**, distinct from the API's `last_checked_at`
field on purpose. The two may show different values at any moment and that
is correct, not a bug — one is this request's timestamp, the other is the
sweep's.

`wa_sweep_claim_due_sessions(p_stale_minutes int, p_limit int)` is
`SECURITY DEFINER` for the same reason `sweep_stale_wa_sends` is: the
supervisor process sets no `app.tenant_id`, and `wa_sessions` carries `FORCE
ROW LEVEL SECURITY`, so an unscoped read from the app role would return zero
rows, silently, rather than erroring where anyone would notice. Unlike the
send sweep, this function only *claims* rows (locks them, stamps
`last_liveness_check_at = now()`, returns `id`/`tenant_id`/`user_id`) — it is
the worker task, not this function, that calls the gateway per claimed
session, and only for the sessions the gateway actually answered does the
timestamp mean anything (see the task docstring for why a claim is stamped
speculatively and what happens on an unreachable gateway).

Only `connected` and `reconnecting` sessions are ever selected — the state
machine's two "claims to be live" states (plan §6). A `disconnected` or
`logged_out` session has nothing to check, and asking the gateway about one
would make the gateway wake a socket nobody asked it to hold open.

Revision ID: f2b6a1d9c4e8
Revises: e1a9c5f3d7b2
Create Date: 2026-07-29 24:00:00+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from app.core.config import settings

revision: str = "f2b6a1d9c4e8"
down_revision: str | None = "e1a9c5f3d7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "wa_sweep_claim_due_sessions(p_stale_minutes int, p_limit int)"
ARG_TYPES = "int, int"
BODY = """
RETURNS TABLE (id uuid, tenant_id uuid, user_id uuid, previous_check_at timestamptz)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- SECURITY DEFINER, RLS bypassed — same "do not trust your own
    -- arguments" reasoning as sweep_stale_wa_sends: EXECUTE is granted to
    -- the application role, the same role a web request runs as.
    --
    -- Floor at 1 minute / cap at 500, same reasoning as sweep_stale_wa_sends:
    -- a p_stale_minutes of 0 would re-claim a session the previous tick just
    -- checked, and an unbounded limit holds FOR UPDATE SKIP LOCKED too long.
    --
    -- Only 'connected'/'reconnecting' — the two states that claim liveness.
    -- 'disconnected'/'logged_out' have nothing to check, and the point of
    -- this sweep is to notice a live-claiming socket that quietly died, not
    -- to poke a session nobody asked to keep open.
    --
    -- The stamp happens here, at claim time, not after the caller's gateway
    -- call succeeds: two sweep workers running concurrently must not both
    -- pick the same row (that is what FOR UPDATE SKIP LOCKED is for), and
    -- claiming is itself the compare-and-set. The worker task is the one
    -- that decides whether the claim was "real" — on an unreachable gateway
    -- it writes nothing further, so the stamp this function wrote survives
    -- unless the task explicitly reverts it. See
    -- app.workers.tasks.sweep_wa_liveness for why an unreachable gateway
    -- must not leave `now()` behind as if a check had actually happened.
    WITH locked AS (
        SELECT id, last_liveness_check_at AS prev FROM wa_sessions
        WHERE status IN ('connected', 'reconnecting')
          AND (
            last_liveness_check_at IS NULL
            OR last_liveness_check_at < now() - make_interval(mins => GREATEST(p_stale_minutes, 1))
          )
        ORDER BY last_liveness_check_at NULLS FIRST
        LIMIT LEAST(GREATEST(p_limit, 1), 500)
        FOR UPDATE SKIP LOCKED
    )
    UPDATE wa_sessions w
    SET last_liveness_check_at = now()
    FROM locked l
    WHERE w.id = l.id
    RETURNING w.id, w.tenant_id, w.user_id, l.prev
$$
"""

REVERT_SIGNATURE = "wa_sweep_revert_check(p_id uuid, p_previous timestamptz)"
REVERT_ARG_TYPES = "uuid, timestamptz"
REVERT_BODY = """
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- The gateway was unreachable for a session this sweep just claimed
    -- (stamped last_liveness_check_at = now() to win the SKIP LOCKED race).
    -- §15: we did not actually check, so the stamp must not survive — this
    -- restores whatever last_liveness_check_at held before the claim
    -- (possibly NULL), rather than asserting a check that never happened.
    UPDATE wa_sessions SET last_liveness_check_at = p_previous WHERE id = p_id
$$
"""


def upgrade() -> None:
    op.add_column(
        "wa_sessions",
        sa.Column("last_liveness_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {BODY}")
    op.execute(f"REVOKE ALL ON FUNCTION wa_sweep_claim_due_sessions({ARG_TYPES}) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' wa_sweep_claim_due_sessions({ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )
    op.execute(f"CREATE OR REPLACE FUNCTION {REVERT_SIGNATURE} {REVERT_BODY}")
    op.execute(
        f"REVOKE ALL ON FUNCTION wa_sweep_revert_check({REVERT_ARG_TYPES}) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' wa_sweep_revert_check({REVERT_ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS wa_sweep_revert_check({REVERT_ARG_TYPES})")
    op.execute(f"DROP FUNCTION IF EXISTS wa_sweep_claim_due_sessions({ARG_TYPES})")
    op.drop_column("wa_sessions", "last_liveness_check_at")
