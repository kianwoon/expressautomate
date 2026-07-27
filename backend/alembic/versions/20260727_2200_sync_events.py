"""sync activity log

Revision ID: c41d8f2b7a05
Revises: e3a7c60fd914
Create Date: 2026-07-27 14:10:00.000000+00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'c41d8f2b7a05'
down_revision: str | None = 'e3a7c60fd914'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same list-and-loop as the extraction migration, for the same reason:
# `verify_rls_enforced()` refuses to boot if a readable table has no forced
# policy, so a table added to the models and forgotten here stops the deploy
# rather than quietly serving every agency's rows.
#
# This one records which agency's mailbox synced and when, which is a picture
# of another agency's hiring activity even without a single email body in it.
PROTECTED: list[tuple[str, str]] = [
    ("sync_events", "tenant_id"),
]

SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        'sync_events',
        sa.Column('mailbox_id', sa.UUID(), nullable=True),
        sa.Column('kind', sa.String(length=48), nullable=False),
        sa.Column('outcome', sa.String(length=16), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['mailbox_id'], ['mailboxes.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_sync_events_mailbox_id'), 'sync_events', ['mailbox_id'], unique=False)
    op.create_index(op.f('ix_sync_events_tenant_id'), 'sync_events', ['tenant_id'], unique=False)
    op.create_index('ix_sync_events_tenant_created', 'sync_events', ['tenant_id', 'created_at'], unique=False)

    _enforce_rls()
    _touch_updated_at()


def _touch_updated_at() -> None:
    """Bind the existing trigger, since these rows are written as raw SQL.

    The writer is a worker holding no ORM object — it inserts through
    `text()` like the rest of the ingestion path — so `Timestamps.updated_at`'s
    ORM-side `onupdate` never fires for this table.
    """
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
    """The policy every tenant-scoped table carries, verbatim.

    FORCE, not merely ENABLE: without it the table owner bypasses the policy,
    and the owner is who migrations and any superuser session connect as — so
    an ENABLE-only table looks protected in the catalogue and is not.
    """
    for table, column in PROTECTED:
        # nullif is load-bearing — see the original migration. Once set_config
        # has run on a connection the setting stays defined for that backend,
        # so an unscoped transaction reads back '' rather than NULL, and
        # casting '' to uuid raises instead of matching nothing.
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
    op.drop_index('ix_sync_events_tenant_created', table_name='sync_events')
    op.drop_index(op.f('ix_sync_events_tenant_id'), table_name='sync_events')
    op.drop_index(op.f('ix_sync_events_mailbox_id'), table_name='sync_events')
    op.drop_table('sync_events')
