"""candidate access requests

Revision ID: c1a0d5e7b203
Revises: c1a0d5e7b202
Create Date: 2026-07-31 10:20:00+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c1a0d5e7b203'
down_revision: str | None = 'c1a0d5e7b202'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "app.tenant_id"
PROTECTED = (("candidate_access_requests", "tenant_id"),)


def upgrade() -> None:
    op.create_table(
        'candidate_access_requests',
        sa.Column('candidate_id', sa.UUID(), nullable=False),
        sa.Column('requested_by_user_id', sa.UUID(), nullable=False),
        sa.Column(
            'status', sa.String(length=16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by_user_id', sa.UUID(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'granted', 'declined')",
            name='ck_candidate_access_requests_status_known',
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (resolved_at IS NULL)",
            name='ck_candidate_access_requests_resolution_paired',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'candidate_id'], ['candidates.tenant_id', 'candidates.id'],
            name='fk_candidate_access_requests_candidate_same_tenant', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'requested_by_user_id'], ['users.tenant_id', 'users.id'],
            name='fk_candidate_access_requests_asker_same_tenant', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'resolved_by_user_id'], ['users.tenant_id', 'users.id'],
            name='fk_candidate_access_requests_resolver_same_tenant',
            ondelete='SET NULL (resolved_by_user_id)',
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_candidate_access_requests_candidate_id'), 'candidate_access_requests',
        ['candidate_id'], unique=False,
    )
    op.create_index(
        op.f('ix_candidate_access_requests_requested_by_user_id'), 'candidate_access_requests',
        ['requested_by_user_id'], unique=False,
    )
    op.create_index(
        op.f('ix_candidate_access_requests_tenant_id'), 'candidate_access_requests',
        ['tenant_id'], unique=False,
    )
    op.create_index(
        'uq_candidate_access_requests_one_pending', 'candidate_access_requests',
        ['tenant_id', 'candidate_id', 'requested_by_user_id'],
        unique=True, postgresql_where=sa.text("status = 'pending'"),
    )

    _enforce_rls()


def _enforce_rls() -> None:
    """The same policy every tenant-scoped table carries, for the same reasons.

    FORCE, not merely ENABLE: without it the table owner bypasses the policy,
    and the owner is who migrations and any superuser session connect as.

    No GRANT is needed — `ALTER DEFAULT PRIVILEGES` in
    20260726_1800_row_level_security.py already gives the runtime role DML on
    every new table. That is exactly why omitting this policy would be
    dangerous rather than merely incomplete: the role can read the table the
    moment it exists.
    """
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


def downgrade() -> None:
    op.drop_table('candidate_access_requests')
