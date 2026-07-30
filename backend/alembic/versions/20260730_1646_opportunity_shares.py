"""opportunity shares

Revision ID: 314cc3da9ced
Revises: a1b2c3d4e5f6
Create Date: 2026-07-30 16:46:34.169848+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '314cc3da9ced'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "app.tenant_id"
PROTECTED = (("opportunity_shares", "tenant_id"),)


def upgrade() -> None:
    op.create_table(
        'opportunity_shares',
        sa.Column('opportunity_id', sa.UUID(), nullable=False),
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
            "scope IN ('user', 'tenant')", name='ck_opportunity_shares_scope_known'
        ),
        sa.CheckConstraint(
            "(scope = 'tenant') = (shared_with_user_id IS NULL)",
            name='ck_opportunity_shares_scope_target',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'opportunity_id'], ['opportunities.tenant_id', 'opportunities.id'],
            name='fk_opportunity_shares_opportunity_same_tenant', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'shared_with_user_id'], ['users.tenant_id', 'users.id'],
            name='fk_opportunity_shares_recipient_same_tenant', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'shared_by_user_id'], ['users.tenant_id', 'users.id'],
            name='fk_opportunity_shares_sharer_same_tenant',
            ondelete='SET NULL (shared_by_user_id)',
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_opportunity_shares_opportunity_id'), 'opportunity_shares', ['opportunity_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_opportunity_shares_shared_with_user_id'), 'opportunity_shares',
        ['shared_with_user_id'], unique=False,
    )
    op.create_index(
        op.f('ix_opportunity_shares_tenant_id'), 'opportunity_shares', ['tenant_id'],
        unique=False,
    )
    op.create_index(
        'uq_opportunity_shares_per_user', 'opportunity_shares',
        ['tenant_id', 'opportunity_id', 'shared_with_user_id'],
        unique=True, postgresql_where=sa.text("scope = 'user'"),
    )
    op.create_index(
        'uq_opportunity_shares_per_tenant', 'opportunity_shares',
        ['tenant_id', 'opportunity_id'],
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
    op.drop_table('opportunity_shares')
