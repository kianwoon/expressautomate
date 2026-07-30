"""client administration

Revision ID: a0bfc93f7eb8
Revises: 6b1e9f4d7a20
Create Date: 2026-07-30 07:40:12.131306+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a0bfc93f7eb8'
down_revision: str | None = '6b1e9f4d7a20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "app.tenant_id"
PROTECTED = (("client_contacts", "tenant_id"),)


def upgrade() -> None:
    op.create_table(
        'client_contacts',
        sa.Column('client_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('email', sa.Text(), nullable=True),
        sa.Column('phone', sa.Text(), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('is_primary', sa.Boolean(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'client_id'], ['clients.tenant_id', 'clients.id'],
            name='fk_client_contacts_client_same_tenant', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_client_contacts_client_id'), 'client_contacts', ['client_id'], unique=False)
    op.create_index(op.f('ix_client_contacts_tenant_id'), 'client_contacts', ['tenant_id'], unique=False)
    op.create_index(
        'uq_client_contacts_one_primary', 'client_contacts', ['tenant_id', 'client_id'],
        unique=True, postgresql_where=sa.text('is_primary'),
    )

    op.add_column('clients', sa.Column('website', sa.Text(), nullable=True))
    op.add_column('clients', sa.Column('phone', sa.Text(), nullable=True))
    op.add_column('clients', sa.Column('address', sa.Text(), nullable=True))
    op.add_column('clients', sa.Column('fee_percent', sa.Numeric(precision=5, scale=2), nullable=True))
    op.add_column('clients', sa.Column('payment_terms_days', sa.Integer(), nullable=True))
    op.add_column('clients', sa.Column('notes', sa.Text(), nullable=True))
    op.add_column('clients', sa.Column('suspended_reason', sa.Text(), nullable=True))
    op.add_column('clients', sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True))
    # NOT NULL on a table with existing rows, so it needs a server default at
    # add-time.
    op.add_column(
        'clients',
        sa.Column('source', sa.String(length=16), nullable=False, server_default='pipeline'),
    )

    op.drop_constraint('ck_clients_status', 'clients', type_='check')
    op.create_check_constraint(
        'ck_clients_status',
        'clients',
        "status IN ('unconfirmed', 'confirmed', 'suspended', 'merged', 'archived')",
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
    op.drop_constraint('ck_clients_status', 'clients', type_='check')
    op.create_check_constraint(
        'ck_clients_status',
        'clients',
        "status IN ('unconfirmed', 'confirmed', 'merged', 'archived')",
    )

    op.drop_column('clients', 'source')
    op.drop_column('clients', 'suspended_at')
    op.drop_column('clients', 'suspended_reason')
    op.drop_column('clients', 'notes')
    op.drop_column('clients', 'payment_terms_days')
    op.drop_column('clients', 'fee_percent')
    op.drop_column('clients', 'address')
    op.drop_column('clients', 'phone')
    op.drop_column('clients', 'website')

    # The policy goes with the table.
    op.drop_table('client_contacts')
