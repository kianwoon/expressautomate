"""candidate shares

Revision ID: c1a0d5e7b202
Revises: c1a0d5e7b201
Create Date: 2026-07-31 10:10:00+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c1a0d5e7b202'
down_revision: str | None = 'c1a0d5e7b201'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "app.tenant_id"
PROTECTED = (("candidate_shares", "tenant_id"),)


def upgrade() -> None:
    op.create_table(
        'candidate_shares',
        sa.Column('candidate_id', sa.UUID(), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('shared_with_user_id', sa.UUID(), nullable=True),
        sa.Column('shared_by_user_id', sa.UUID(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.CheckConstraint(
            "scope IN ('user', 'tenant')", name='ck_candidate_shares_scope_known'
        ),
        sa.CheckConstraint(
            "(scope = 'tenant') = (shared_with_user_id IS NULL)",
            name='ck_candidate_shares_scope_target',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'candidate_id'], ['candidates.tenant_id', 'candidates.id'],
            name='fk_candidate_shares_candidate_same_tenant', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'shared_with_user_id'], ['users.tenant_id', 'users.id'],
            name='fk_candidate_shares_recipient_same_tenant', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'shared_by_user_id'], ['users.tenant_id', 'users.id'],
            name='fk_candidate_shares_sharer_same_tenant',
            ondelete='SET NULL (shared_by_user_id)',
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_candidate_shares_candidate_id'), 'candidate_shares', ['candidate_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_candidate_shares_shared_with_user_id'), 'candidate_shares',
        ['shared_with_user_id'], unique=False,
    )
    op.create_index(
        op.f('ix_candidate_shares_tenant_id'), 'candidate_shares', ['tenant_id'],
        unique=False,
    )
    op.create_index(
        'uq_candidate_shares_per_user', 'candidate_shares',
        ['tenant_id', 'candidate_id', 'shared_with_user_id'],
        unique=True, postgresql_where=sa.text("scope = 'user'"),
    )
    op.create_index(
        'uq_candidate_shares_per_tenant', 'candidate_shares',
        ['tenant_id', 'candidate_id'],
        unique=True, postgresql_where=sa.text("scope = 'tenant'"),
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
    op.drop_table('candidate_shares')
