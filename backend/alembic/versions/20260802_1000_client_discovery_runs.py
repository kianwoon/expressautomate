"""client discovery runs

Revision ID: c1a0d5e7b207
Revises: c1a0d5e7b206
Create Date: 2026-08-02 10:00:00+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'c1a0d5e7b207'
down_revision: str | None = 'c1a0d5e7b206'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "app.tenant_id"
PROTECTED = (("client_discovery_runs", "tenant_id"),)


def upgrade() -> None:
    op.create_table(
        'client_discovery_runs',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('lookback_days', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('inbox_scanned', sa.Integer(), nullable=False),
        sa.Column('sent_scanned', sa.Integer(), nullable=False),
        sa.Column('messages_truncated', sa.Boolean(), nullable=False),
        sa.Column('domains_truncated', sa.Boolean(), nullable=False),
        sa.Column('clients_enriched', sa.Integer(), nullable=False),
        sa.Column('contacts_added', sa.Integer(), nullable=False),
        sa.Column('results', JSONB(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name='ck_client_discovery_runs_status',
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'user_id'], ['users.tenant_id', 'users.id'],
            name='fk_client_discovery_runs_user_same_tenant', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_client_discovery_runs_tenant_id'), 'client_discovery_runs',
        ['tenant_id'], unique=False,
    )
    op.create_index(
        op.f('ix_client_discovery_runs_user_id'), 'client_discovery_runs',
        ['user_id'], unique=False,
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
    op.drop_table('client_discovery_runs')
