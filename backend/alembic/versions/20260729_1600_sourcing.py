"""sourcing tables

Creates `sourcing_runs`, `sourcing_matches`, and `candidate_submissions` —
the three tables Task 1 of candidate sourcing needs before any scoring,
eligibility, or model pass exists. RLS is enabled and forced in this same
revision, for the reason every prior sourcing-adjacent migration gives:
`verify_rls_enforced()` refuses to boot on a readable table without a forced
policy, so adding the policy later would be a silent cross-tenant leak in
the gap between "table exists" and "policy exists".

`candidate_submissions` is unique on `(tenant_id, candidate_id, client_id)`
— a person is either in front of a client or not, so a double-click (or a
rerun of the job that produced the recommendation) must not create a second
row. It carries no status column: this table answers one boolean question,
not a workflow.

`sourcing_runs.protected_attribute_noticed`/`_note`: the model that produces
a run (Task 5) is instructed to report a plainly discriminatory requirement
it reads in the job order. These columns are where that report lands — see
`app/models/sourcing.py` for the fuller rationale.

`opportunity_id` on both `sourcing_runs` and `candidate_submissions` is a
plain (non-composite) FK, not the `(tenant_id, ...)` composite idiom used for
`candidate_id`/`client_id`/`run_id` below: `opportunities` has no
`(tenant_id, id)` unique constraint for a composite FK to reference, the same
situation `CandidateImport.import_id` already documents in
`20260729_1300_candidate_imports.py`.

Revision ID: c1d4e8f29a3b
Revises: c8e2b47d5a91
Create Date: 2026-07-29 16:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "c1d4e8f29a3b"
down_revision: str | None = "c8e2b47d5a91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTING = "app.tenant_id"


def _enable_rls(table: str) -> None:
    predicate = f"tenant_id = nullif(current_setting('{_SETTING}', true), '')::uuid"
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


def upgrade() -> None:
    op.create_table(
        "sourcing_runs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opportunity_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("candidates_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shortlisted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_name", sa.Text()),
        sa.Column("prompt_version", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "protected_attribute_noticed",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("protected_attribute_note", sa.Text()),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_sourcing_runs_tenant_id_id"),
        sa.CheckConstraint(
            "state IN ('pending','running','done','failed')",
            name="ck_sourcing_runs_state",
        ),
    )
    op.create_index("ix_sourcing_runs_tenant_id", "sourcing_runs", ["tenant_id"])
    op.create_index("ix_sourcing_runs_opportunity_id", "sourcing_runs", ["opportunity_id"])

    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON sourcing_runs TO "{settings.DATABASE_APP_ROLE}"'
    )
    _enable_rls("sourcing_runs")

    op.create_table(
        "sourcing_matches",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Numeric(5, 2), nullable=False),
        sa.Column("reasons", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("explanation", sa.Text()),
        sa.Column("explanation_evidence", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            name="fk_sourcing_matches_run_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_sourcing_matches_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "candidate_id", name="uq_sourcing_matches_once_per_run"
        ),
    )
    op.create_index("ix_sourcing_matches_tenant_id", "sourcing_matches", ["tenant_id"])
    op.create_index("ix_sourcing_matches_run_id", "sourcing_matches", ["run_id"])

    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON sourcing_matches '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )
    _enable_rls("sourcing_matches")

    op.create_table(
        "candidate_submissions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opportunity_id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("submitted_by", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_submissions_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_candidate_submissions_client_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["opportunities.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "client_id",
            name="uq_candidate_submissions_once_per_client",
        ),
    )
    op.create_index(
        "ix_candidate_submissions_tenant_id", "candidate_submissions", ["tenant_id"]
    )

    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON candidate_submissions '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )
    _enable_rls("candidate_submissions")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_submissions")
    op.drop_table("candidate_submissions")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON sourcing_matches")
    op.drop_table("sourcing_matches")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON sourcing_runs")
    op.drop_table("sourcing_runs")
