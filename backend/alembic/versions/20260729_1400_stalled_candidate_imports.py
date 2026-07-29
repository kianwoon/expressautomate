"""stalled candidate imports

The imports third of `rescan_stuck`. A SECURITY DEFINER resolver for the same
reason `stalled_candidate_documents` and `stalled_email_rows` are: the sweep
runs across every tenant at once with no `app.tenant_id` set, and
`candidate_imports` carries FORCE ROW LEVEL SECURITY, so a plain SELECT there
matches zero rows silently rather than erroring — a recovery net that appears
to run while doing nothing.

Without this, a worker killed mid-import strands the uploaded file for ever:
`enqueue` returning `False` is caught at the upload, but a process that dies
after taking the job returns nothing at all.

Revision ID: a4d1c6b39e52
Revises: f3a8c92d1e07
Create Date: 2026-07-29 14:00:00+00:00
"""
from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "a4d1c6b39e52"
down_revision: str | None = "f3a8c92d1e07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESOLVER = "stalled_candidate_imports(p_pending_minutes int, p_working_minutes int)"
_RESOLVER_ARGS = "int, int"
_RESOLVER_BODY = """
    RETURNS TABLE (id uuid, tenant_id uuid, state text)
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
        -- tenant_id travels with the row because `run_candidate_import`
        -- takes it: enqueueing the import id alone would raise TypeError
        -- inside the worker, on the far side of the queue, where nothing is
        -- watching.
        --
        -- Two grace periods, matching the other two resolvers: a queue hop
        -- should be quick, but a genuine five-hundred-row import spends real
        -- time in `parsing`. `done`, `failed` and `undone` are absent on
        -- purpose — they are answers, not interruptions.
        SELECT i.id, i.tenant_id, i.state
        FROM candidate_imports i
        WHERE (i.state = 'pending'
               AND i.updated_at < now() - make_interval(mins => p_pending_minutes))
           OR (i.state = 'parsing'
               AND i.updated_at < now() - make_interval(mins => p_working_minutes))
    $$
"""


def upgrade() -> None:
    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"CREATE OR REPLACE FUNCTION {_RESOLVER} {_RESOLVER_BODY}")
    # Postgres grants EXECUTE to PUBLIC by default, which would hand an
    # RLS-bypassing function to every role that can connect.
    op.execute(f"REVOKE ALL ON FUNCTION {name}({_RESOLVER_ARGS}) FROM PUBLIC")
    op.execute(
        f'GRANT EXECUTE ON FUNCTION {name}({_RESOLVER_ARGS}) '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"DROP FUNCTION IF EXISTS {name}({_RESOLVER_ARGS})")
