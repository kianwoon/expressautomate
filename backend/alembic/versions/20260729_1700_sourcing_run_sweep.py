"""sourcing run sweep, and a score column wide enough for the score

Two changes the run job needs before it can be correct.

`sourcing_matches.score` was `NUMERIC(5, 2)` while the scorer computes to
`SOURCING_SCORE_DECIMAL_PLACES` (four). Stored at two places, candidates whose
scores genuinely differ come back as ties, and a shortlist ordered by score
then reorders itself between two readings of the same stored run — which is
precisely the property a run being a record rather than a live query is
supposed to buy. Widened to `NUMERIC(6, 4)`: the value is a mean of signals
in [0, 1], so one integer digit is one more than it can use.

`stalled_sourcing_runs` is the fourth resolver `rescan_stuck` reads, and a
`SECURITY DEFINER` one for exactly the reason `stalled_candidate_imports` is:
the sweep runs across every tenant at once with no `app.tenant_id` set, and
`sourcing_runs` carries FORCE ROW LEVEL SECURITY, so a plain SELECT there
matches zero rows silently rather than erroring — a recovery net that appears
to run while doing nothing. It returns ids and state only, never any of the
run's content: the job re-reads the row under its own tenant's policy.

Revision ID: d2f6a41b8c73
Revises: c1d4e8f29a3b
Create Date: 2026-07-29 17:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "d2f6a41b8c73"
down_revision: str | None = "c1d4e8f29a3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESOLVER = "stalled_sourcing_runs(p_pending_minutes int, p_working_minutes int)"
_RESOLVER_ARGS = "int, int"
_RESOLVER_BODY = """
    RETURNS TABLE (id uuid, tenant_id uuid, opportunity_id uuid, state text)
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
        -- tenant_id and opportunity_id travel with the row because
        -- `run_sourcing` takes all three: enqueueing the run id alone would
        -- raise TypeError inside the worker, on the far side of the queue,
        -- where nothing is watching.
        --
        -- Routing ids and the state, and nothing else. A job order's title
        -- and a candidate's name are tenant content, and this function reads
        -- past every tenant policy there is; ids are the most it can hand
        -- back without becoming a way to read another agency's data.
        --
        -- Two grace periods, matching the other three resolvers: a queue hop
        -- should be quick, but a real run scores an agency's whole candidate
        -- database and then spends a model call. `done` and `failed` are
        -- absent on purpose — they are answers, not interruptions.
        SELECT r.id, r.tenant_id, r.opportunity_id, r.state
        FROM sourcing_runs r
        WHERE (r.state = 'pending'
               AND r.updated_at < now() - make_interval(mins => p_pending_minutes))
           OR (r.state = 'running'
               AND r.updated_at < now() - make_interval(mins => p_working_minutes))
    $$
"""


def upgrade() -> None:
    op.alter_column(
        "sourcing_matches",
        "score",
        existing_type=sa.Numeric(5, 2),
        type_=sa.Numeric(6, 4),
        existing_nullable=False,
    )
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
    op.alter_column(
        "sourcing_matches",
        "score",
        existing_type=sa.Numeric(6, 4),
        type_=sa.Numeric(5, 2),
        existing_nullable=False,
    )
