"""job intelligence state machine

Revision ID: c1a0d5e7b217
Revises: c1a0d5e7b216
Create Date: 2026-08-06 11:00:00+00:00

Adds the `state` / `failure_reason` / `attempts` columns to `job_intelligence`
and relaxes the three result columns to nullable, so the row can exist in a
`pending` or `running` state before any analysis has been written.

The analysis now runs as an arq job (the three LLM calls have no business
inside an HTTP request, and they must run in the worker process where Cerebras
is configured — the api process has no LLM credentials). The row therefore has
to outlive the request that created it: `pending` from POST until the worker
claims it, then `done` or `failed`. Mirrors `sourcing_runs` exactly.

Also adds `stalled_job_intelligence`, the SECURITY DEFINER resolver
`rescan_stuck` reads to re-enqueue rows a lost enqueue or killed worker stranded
— the same pattern `stalled_sourcing_runs` set, and for the same reason: the
sweep runs across every tenant with no `app.tenant_id`, and `job_intelligence`
carries FORCE ROW LEVEL SECURITY, so a plain SELECT matches nothing silently.

Existing rows — created by the earlier synchronous version, all of them
completed analyses — are backfilled to `state='done'` so they read back as
finished work rather than stuck at `pending`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "c1a0d5e7b217"
down_revision: str | None = "c1a0d5e7b216"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESOLVER = "stalled_job_intelligence(p_pending_minutes int, p_working_minutes int)"
_RESOLVER_ARGS = "int, int"
_RESOLVER_BODY = """
    RETURNS TABLE (id uuid, tenant_id uuid, opportunity_id uuid, state text)
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
        SELECT r.id, r.tenant_id, r.opportunity_id, r.state
        FROM job_intelligence r
        WHERE (r.state = 'pending'
               AND r.updated_at < now() - make_interval(mins => p_pending_minutes))
           OR (r.state = 'running'
               AND r.updated_at < now() - make_interval(mins => p_working_minutes))
    $$
"""


def upgrade() -> None:
    op.add_column(
        "job_intelligence",
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
    )
    op.add_column("job_intelligence", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.add_column(
        "job_intelligence",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        op.f("ix_job_intelligence_state"), "job_intelligence", ["state"], unique=False
    )
    op.create_check_constraint(
        "ck_job_intelligence_state",
        "job_intelligence",
        "state IN ('pending','running','done','failed')",
    )

    # Any row created by the synchronous version is a completed analysis with
    # all three result columns populated. Mark it `done` so it reads back as
    # finished rather than sitting at the `pending` default forever.
    op.execute(
        "UPDATE job_intelligence SET state = 'done' WHERE understanding IS NOT NULL"
    )

    # The three result columns were NOT NULL because the synchronous route
    # wrote them in one shot. The worker writes them only on success, so they
    # must be nullable for a row that is still `pending` or has `failed`.
    op.alter_column("job_intelligence", "understanding", nullable=True)
    op.alter_column("job_intelligence", "persona", nullable=True)
    op.alter_column("job_intelligence", "search_plan", nullable=True)

    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"CREATE OR REPLACE FUNCTION {_RESOLVER} {_RESOLVER_BODY}")
    op.execute(f"REVOKE ALL ON FUNCTION {name}({_RESOLVER_ARGS}) FROM PUBLIC")
    op.execute(
        f'GRANT EXECUTE ON FUNCTION {name}({_RESOLVER_ARGS}) '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"DROP FUNCTION IF EXISTS {name}({_RESOLVER_ARGS})")
    op.alter_column("job_intelligence", "search_plan", nullable=False)
    op.alter_column("job_intelligence", "persona", nullable=False)
    op.alter_column("job_intelligence", "understanding", nullable=False)
    op.drop_constraint("ck_job_intelligence_state", "job_intelligence", type_="check")
    op.drop_index(op.f("ix_job_intelligence_state"), table_name="job_intelligence")
    op.drop_column("job_intelligence", "attempts")
    op.drop_column("job_intelligence", "failure_reason")
    op.drop_column("job_intelligence", "state")

