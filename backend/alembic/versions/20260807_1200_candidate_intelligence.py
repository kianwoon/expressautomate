"""candidate intelligence table

Revision ID: 4989e08d79a5
Revises: 4989e08d79a4
Create Date: 2026-08-07 12:00:00+00:00

Adds `candidate_intelligence`, one row per candidate holding the latest
three-stage Candidate Intelligence analysis (career / capability / profile).
Re-running the analysis upserts on `(tenant_id, candidate_id)` so the row is an
UPDATE, not a second insert — the same shape `job_intelligence` takes.

Created with the state machine from the start (unlike `job_intelligence`, which
retrofitted it in a second migration): the analysis always runs as an arq job
in the worker, so the row has to outlive the request that created it. The
`stalled_candidate_intelligence` SECURITY DEFINER resolver is added here too, so
`rescan_stuck` can re-enqueue rows a lost enqueue or killed worker stranded —
the same pattern `stalled_job_intelligence` set.

RLS-protected like every other tenant-scoped table: `verify_rls_enforced()`
refuses to start the app if a `tenant_id` table has no policy, so this table
must carry a `tenant_isolation` policy or the deployment will not boot.

Composite FK `(tenant_id, candidate_id) → candidates(tenant_id, id)` with
CASCADE, possible because `candidates` has the `uq_candidates_tenant_id_id`
unique constraint that `opportunities` lacks.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.config import settings

revision: str = "4989e08d79a5"
down_revision: str | None = "4989e08d79a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# See `20260727_1900_extraction_tables.py` for the RLS-enforcement pattern.
PROTECTED: list[tuple[str, str]] = [("candidate_intelligence", "tenant_id")]
SETTING = "app.tenant_id"

_RESOLVER = "stalled_candidate_intelligence(p_pending_minutes int, p_working_minutes int)"
_RESOLVER_ARGS = "int, int"
_RESOLVER_BODY = """
    RETURNS TABLE (id uuid, tenant_id uuid, candidate_id uuid, state text)
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
        SELECT r.id, r.tenant_id, r.candidate_id, r.state
        FROM candidate_intelligence r
        WHERE (r.state = 'pending'
               AND r.updated_at < now() - make_interval(mins => p_pending_minutes))
           OR (r.state = 'running'
               AND r.updated_at < now() - make_interval(mins => p_working_minutes))
    $$
"""


def upgrade() -> None:
    op.create_table(
        "candidate_intelligence",
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        # State machine present from the start: the analysis runs as a job.
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        # The three result columns are nullable: a row that is `pending` or has
        # `failed` carries no analysis. Written only on success.
        sa.Column("career", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("capability", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("analysed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        # Composite FK to `candidates(tenant_id, id)`, possible because
        # `candidates` carries `uq_candidates_tenant_id_id`.
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
            name="fk_candidate_intelligence_candidate_same_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            name="uq_candidate_intelligence_one_per_candidate",
        ),
        sa.CheckConstraint(
            "state IN ('pending','running','done','failed')",
            name="ck_candidate_intelligence_state",
        ),
    )
    op.create_index(
        op.f("ix_candidate_intelligence_candidate_id"),
        "candidate_intelligence",
        ["candidate_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_candidate_intelligence_state"),
        "candidate_intelligence",
        ["state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_candidate_intelligence_tenant_id"),
        "candidate_intelligence",
        ["tenant_id"],
        unique=False,
    )

    _enforce_rls()
    _touch_updated_at()
    _create_resolver()


def downgrade() -> None:
    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"DROP FUNCTION IF EXISTS {name}({_RESOLVER_ARGS})")
    op.drop_index(
        op.f("ix_candidate_intelligence_tenant_id"), table_name="candidate_intelligence"
    )
    op.drop_index(
        op.f("ix_candidate_intelligence_state"), table_name="candidate_intelligence"
    )
    op.drop_index(
        op.f("ix_candidate_intelligence_candidate_id"),
        table_name="candidate_intelligence",
    )
    op.drop_table("candidate_intelligence")


def _touch_updated_at() -> None:
    """Bind the shared trigger so `updated_at` means what every table's does."""
    op.execute(
        "DROP TRIGGER IF EXISTS candidate_intelligence_touch_updated_at "
        "ON candidate_intelligence"
    )
    op.execute(
        """
        CREATE TRIGGER candidate_intelligence_touch_updated_at
        BEFORE UPDATE ON candidate_intelligence
        FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
        """
    )


def _enforce_rls() -> None:
    """FORCE, not merely ENABLE — see `20260727_1900_extraction_tables.py`."""
    for table, column in PROTECTED:
        predicate = f"{column} = nullif(current_setting('{SETTING}', true), '')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )


def _create_resolver() -> None:
    """The SECURITY DEFINER resolver `rescan_stuck` reads.

    `candidate_intelligence` carries FORCE ROW LEVEL SECURITY, and the sweep
    runs across every tenant with no `app.tenant_id` set, so a direct SELECT
    would match nothing — silently, since RLS filters rather than errors. The
    resolver reads across tenants with elevated rights, the same pattern
    `stalled_job_intelligence` set.
    """
    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"CREATE OR REPLACE FUNCTION {_RESOLVER} {_RESOLVER_BODY}")
    op.execute(f"REVOKE ALL ON FUNCTION {name}({_RESOLVER_ARGS}) FROM PUBLIC")
    op.execute(
        f'GRANT EXECUTE ON FUNCTION {name}({_RESOLVER_ARGS}) '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )
