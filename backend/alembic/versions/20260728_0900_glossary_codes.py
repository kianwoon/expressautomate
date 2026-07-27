"""glossary codes and seed ledger

Revision ID: b7f3c1d94e28
Revises: c41d8f2b7a05
Create Date: 2026-07-28 09:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b7f3c1d94e28'
down_revision: str | None = 'c41d8f2b7a05'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Both tables carry `tenant_id`, so both need a policy. `verify_rls_enforced()`
# refuses to boot on any readable table without FORCE ROW LEVEL SECURITY — and
# the glossary is exactly the sort of table that would otherwise slip through,
# because it looks like reference data rather than customer data. It is not:
# one agency's shorthand tells a competitor which clients they staff for and
# on what characteristics.
PROTECTED: list[tuple[str, str]] = [
    ("glossary_codes", "tenant_id"),
    ("glossary_seed_marks", "tenant_id"),
]

SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        'glossary_codes',
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('code_normalised', sa.String(length=64), nullable=False),
        sa.Column('meaning', sa.Text(), nullable=False),
        sa.Column('attribute', sa.String(length=32), nullable=True),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # Mirrors `PROTECTED_ATTRIBUTES` and `SOURCES` in app/models/glossary.py.
        # allow-hardcode: a schema enumeration. The API validates against the
        # Python tuple so the caller gets a 400 rather than a 500, and this is
        # the backstop for every writer that does not go through the API.
        sa.CheckConstraint(
            "attribute IS NULL OR attribute IN "
            "('race','nationality','gender','age','religion','marital_status')",
            name='ck_glossary_codes_attribute',
        ),
        sa.CheckConstraint("source IN ('starter','agency')", name='ck_glossary_codes_source'),
        # A code that normalises to nothing ("///") would match every empty
        # string the detector looks at.
        sa.CheckConstraint("code_normalised <> ''", name='ck_glossary_codes_normalised_not_empty'),
        # The constraint the whole design rests on: one meaning per code per
        # agency, decided on the normalised form.
        sa.UniqueConstraint('tenant_id', 'code_normalised', name='uq_glossary_codes_tenant_code'),
    )
    op.create_index(op.f('ix_glossary_codes_tenant_id'), 'glossary_codes', ['tenant_id'], unique=False)

    op.create_table(
        'glossary_seed_marks',
        sa.Column('code_normalised', sa.String(length=64), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'code_normalised', name='uq_glossary_seed_marks_tenant_code'),
    )
    op.create_index(op.f('ix_glossary_seed_marks_tenant_id'), 'glossary_seed_marks', ['tenant_id'], unique=False)

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
    op.drop_index(op.f('ix_glossary_seed_marks_tenant_id'), table_name='glossary_seed_marks')
    op.drop_table('glossary_seed_marks')
    op.drop_index(op.f('ix_glossary_codes_tenant_id'), table_name='glossary_codes')
    op.drop_table('glossary_codes')
