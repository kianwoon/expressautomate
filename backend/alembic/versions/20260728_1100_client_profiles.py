"""client profiles

Revision ID: e5b92d8a7c41
Revises: d4a81c7f6b30
Create Date: 2026-07-28 11:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'e5b92d8a7c41'
down_revision: str | None = 'd4a81c7f6b30'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED: list[tuple[str, str]] = [
    ("clients", "tenant_id"),
    ("client_mentions", "tenant_id"),
]

SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        'clients',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('name_normalized', sa.Text(), nullable=False),
        sa.Column('email_domain', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='unconfirmed'),
        sa.Column('merged_into_client_id', sa.UUID(), nullable=True),
        sa.Column('first_seen_email_message_id', sa.UUID(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['first_seen_email_message_id'], ['email_messages.id'], ondelete='SET NULL'
        ),
        # The target of a child's composite foreign key.
        sa.UniqueConstraint('tenant_id', 'id', name='uq_clients_tenant_id_id'),
        # A merged row that does not say what it merged into is unusable, and a
        # live row pointing somewhere else is a contradiction. Both directions
        # are enforced because only checking one leaves the other reachable.
        sa.CheckConstraint(
            "(status = 'merged') = (merged_into_client_id IS NOT NULL)",
            name='ck_clients_merged_has_target',
        ),
        sa.CheckConstraint(
            "status IN ('unconfirmed', 'confirmed', 'merged', 'archived')",
            name='ck_clients_status',
        ),
    )
    op.create_index(op.f('ix_clients_tenant_id'), 'clients', ['tenant_id'])
    op.create_index(op.f('ix_clients_name_normalized'), 'clients', ['name_normalized'])
    op.create_index(op.f('ix_clients_status'), 'clients', ['status'])
    op.create_index(op.f('ix_clients_last_seen_at'), 'clients', ['last_seen_at'])

    # Self-FK added after the table exists, and composite so a merge target can
    # never be another agency's client.
    op.create_foreign_key(
        'fk_clients_merged_into_same_tenant',
        'clients',
        'clients',
        ['tenant_id', 'merged_into_client_id'],
        ['tenant_id', 'id'],
        ondelete='SET NULL',
    )

    # The domain key. `merged` is excluded so a merge frees the domain for the
    # surviving row; `archived` is deliberately INCLUDED, because the matcher
    # matches archived clients and an excluded archived row would send it to
    # the insert path and straight into a unique violation.
    op.create_index(
        'uq_clients_tenant_domain',
        'clients',
        ['tenant_id', 'email_domain'],
        unique=True,
        postgresql_where=sa.text("email_domain IS NOT NULL AND status <> 'merged'"),
    )

    op.create_table(
        'client_mentions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('client_id', sa.UUID(), nullable=False),
        sa.Column('email_message_id', sa.UUID(), nullable=True),
        sa.Column('matched_by', sa.String(length=16), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'client_id'],
            ['clients.tenant_id', 'clients.id'],
            name='fk_client_mentions_client_same_tenant',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['email_message_id'], ['email_messages.id'], ondelete='SET NULL'
        ),
        # NULLS NOT DISTINCT: without it, Postgres treats every NULL
        # email_message_id as distinct, so ON CONFLICT DO NOTHING never fires
        # for mentions with no message id and reprocessing duplicates them.
        sa.UniqueConstraint(
            'tenant_id', 'client_id', 'email_message_id',
            name='uq_client_mentions_once_per_message',
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "matched_by IN ('email_domain', 'name', 'human')",
            name='ck_client_mentions_matched_by',
        ),
    )
    op.create_index(op.f('ix_client_mentions_tenant_id'), 'client_mentions', ['tenant_id'])
    op.create_index(op.f('ix_client_mentions_client_id'), 'client_mentions', ['client_id'])
    op.create_index(
        op.f('ix_client_mentions_email_message_id'), 'client_mentions', ['email_message_id']
    )

    _enforce_rls()
    _touch_updated_at()


def _touch_updated_at() -> None:
    """Bind the existing trigger, so `updated_at` means the same thing here."""
    for table, _column in PROTECTED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_touch_updated_at ON {table}")
        op.execute(
            f"""
            CREATE TRIGGER {table}_touch_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
            """
        )


def _enforce_rls() -> None:
    """The same policy every tenant-scoped table carries, for the same reasons.

    FORCE, not merely ENABLE: without it the table owner bypasses the policy,
    and the owner is who migrations and any superuser session connect as.
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
    op.drop_table('client_mentions')
    op.drop_index('uq_clients_tenant_domain', table_name='clients')
    op.drop_table('clients')
