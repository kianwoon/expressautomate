"""opportunity codes

Revision ID: d4a81c7f6b30
Revises: b7f3c1d94e28
Create Date: 2026-07-28 09:30:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd4a81c7f6b30'
down_revision: str | None = 'b7f3c1d94e28'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decoded shorthand is the most sensitive thing this product stores: a row here
# says an agency's client asked for a protected characteristic. It carries
# `tenant_id` and therefore needs the policy — `verify_rls_enforced()` refuses
# to boot the app on any readable table without FORCE ROW LEVEL SECURITY.
PROTECTED: list[tuple[str, str]] = [
    ("opportunity_codes", "tenant_id"),
]

SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        'opportunity_codes',
        sa.Column('opportunity_id', sa.UUID(), nullable=False),
        sa.Column('code', sa.String(length=64), nullable=False),
        # Snapshotted, not joined. See the model docstring: an FK to
        # `glossary_codes` would let a later edit rewrite what January's job
        # orders were understood to say.
        sa.Column('meaning', sa.Text(), nullable=False),
        sa.Column('attribute', sa.String(length=32), nullable=True),
        sa.Column('start_char', sa.Integer(), nullable=False),
        sa.Column('end_char', sa.Integer(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # An offset pair that does not describe a forward span cannot be
        # sliced out of the email, and a row nobody can verify is worse than
        # no row: it looks like provenance and is not.
        sa.CheckConstraint('start_char >= 0 AND end_char > start_char', name='ck_opportunity_codes_span'),
    )
    op.create_index(
        op.f('ix_opportunity_codes_opportunity_id'),
        'opportunity_codes',
        ['opportunity_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_opportunity_codes_tenant_id'), 'opportunity_codes', ['tenant_id'], unique=False
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
    and the owner is who migrations and any superuser session connect as — so
    an ENABLE-only table looks protected in the catalogue and is not.
    """
    for table, column in PROTECTED:
        # nullif is load-bearing — once set_config has run on a connection the
        # setting stays defined for that backend, so an unscoped transaction
        # reads back '' rather than NULL, and casting '' to uuid raises instead
        # of matching nothing.
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
    op.drop_index(op.f('ix_opportunity_codes_tenant_id'), table_name='opportunity_codes')
    op.drop_index(op.f('ix_opportunity_codes_opportunity_id'), table_name='opportunity_codes')
    op.drop_table('opportunity_codes')
