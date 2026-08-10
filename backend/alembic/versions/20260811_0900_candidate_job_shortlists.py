"""candidate job shortlist table

Revision ID: c1v2e0000001
Revises: c1v2d0000001
Create Date: 2026-08-11 09:00:00+00:00

Adds `candidate_job_shortlists`, one row per candidate holding the last Find
Job shortlist: the serialized job orders, their scores and the breakdowns
behind them. The Jobs tab in the candidate modal reopens to this snapshot
without re-scoring the agency's vacancies.

Created from the start with RLS, the same way `candidate_intelligence` was:
`verify_rls_enforced()` refuses to boot if a `tenant_id` table has no policy,
so this table must carry a `tenant_isolation` policy or the deployment will
not start. FORCE, not merely ENABLE — see `20260727_1900_extraction_tables.py`.

Composite FK `(tenant_id, candidate_id) → candidates(tenant_id, id)` with
CASCADE, possible because `candidates` carries `uq_candidates_tenant_id_id`.
Re-running Find Job upserts on `(tenant_id, candidate_id)`, so there is one
snapshot per candidate rather than a history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c1v2e0000001"
down_revision: str | None = "c1v2d0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED: list[tuple[str, str]] = [("candidate_job_shortlists", "tenant_id")]
SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "candidate_job_shortlists",
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        # Bookkeeping, mirrored from `SourcingRun`'s pair so the modal can say
        # "best N of M scoreable (of K visible)".
        sa.Column("considered", sa.Integer(), nullable=False),
        sa.Column("scored", sa.Integer(), nullable=False),
        sa.Column("limit", sa.Integer(), nullable=False),
        # The candidate's salary expectation as of the run — null when
        # incomplete, so the salary absence is a candidate-level fact.
        sa.Column("candidate_salary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # The serialized shortlist, exactly as the modal renders it.
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        # Composite FK to `candidates(tenant_id, id)`, possible because
        # `candidates` carries `uq_candidates_tenant_id_id`.
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
            name="fk_candidate_job_shortlists_candidate_same_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            name="uq_candidate_job_shortlists_once_per_candidate",
        ),
    )
    op.create_index(
        op.f("ix_candidate_job_shortlists_tenant_id"),
        "candidate_job_shortlists",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_candidate_job_shortlists_candidate_id"),
        "candidate_job_shortlists",
        ["candidate_id"],
        unique=False,
    )

    _enforce_rls()
    _touch_updated_at()


def downgrade() -> None:
    op.drop_index(
        op.f("ix_candidate_job_shortlists_candidate_id"),
        table_name="candidate_job_shortlists",
    )
    op.drop_index(
        op.f("ix_candidate_job_shortlists_tenant_id"),
        table_name="candidate_job_shortlists",
    )
    op.drop_table("candidate_job_shortlists")


def _touch_updated_at() -> None:
    """Bind the shared trigger so `updated_at` means what every table's does."""
    op.execute(
        "DROP TRIGGER IF EXISTS candidate_job_shortlists_touch_updated_at "
        "ON candidate_job_shortlists"
    )
    op.execute(
        """
        CREATE TRIGGER candidate_job_shortlists_touch_updated_at
        BEFORE UPDATE ON candidate_job_shortlists
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
