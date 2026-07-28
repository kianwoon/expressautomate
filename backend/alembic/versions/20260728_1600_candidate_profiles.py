"""candidate profiles

Revision ID: a2d71b8c4f39
Revises: f1c40a9d5e72
Create Date: 2026-07-28 16:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a2d71b8c4f39'
down_revision: str | None = 'f1c40a9d5e72'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED: list[tuple[str, str]] = [
    ("candidates", "tenant_id"),
    ("candidate_skills", "tenant_id"),
    ("candidate_field_overrides", "tenant_id"),
]

SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        'candidates',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('full_name', sa.Text(), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=True),
        sa.Column('phone_raw', sa.String(length=64), nullable=True),
        sa.Column('phone_e164', sa.String(length=32), nullable=True),
        sa.Column('current_title', sa.Text(), nullable=True),
        sa.Column('current_employer', sa.Text(), nullable=True),
        sa.Column('location', sa.Text(), nullable=True),
        sa.Column('years_experience', sa.Integer(), nullable=True),
        sa.Column('expected_salary', sa.Numeric(12, 2), nullable=True),
        sa.Column('salary_currency', sa.String(length=8), nullable=True),
        sa.Column('salary_period', sa.String(length=16), nullable=True),
        sa.Column('available_from', sa.Date(), nullable=True),
        sa.Column('notice_period_raw', sa.Text(), nullable=True),
        sa.Column('employment_type', sa.String(length=32), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('pipeline_stage', sa.String(length=16), nullable=False, server_default='new'),
        sa.Column('record_status', sa.String(length=16), nullable=False, server_default='active'),
        sa.Column('merged_into_candidate_id', sa.UUID(), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('updated_by', sa.UUID(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ondelete='SET NULL'),
        sa.UniqueConstraint('tenant_id', 'id', name='uq_candidates_tenant_id_id'),
        sa.CheckConstraint(
            "(record_status = 'merged') = (merged_into_candidate_id IS NOT NULL)",
            name='ck_candidates_merged_has_target',
        ),
        sa.CheckConstraint(
            "record_status IN ('active', 'archived', 'merged')",
            name='ck_candidates_record_status',
        ),
        sa.CheckConstraint(
            "pipeline_stage IN ('new', 'contacted', 'submitted', 'placed', 'rejected')",
            name='ck_candidates_pipeline_stage',
        ),
        # A person needs a name. Everything else is optional because a
        # recruiter often has a name and a number and nothing more.
        sa.CheckConstraint("length(btrim(full_name)) > 0", name='ck_candidates_name_not_blank'),
    )
    op.create_index(op.f('ix_candidates_tenant_id'), 'candidates', ['tenant_id'])
    op.create_index(op.f('ix_candidates_pipeline_stage'), 'candidates', ['pipeline_stage'])
    op.create_index(op.f('ix_candidates_record_status'), 'candidates', ['record_status'])
    # CASCADE: a row merged into this one is a duplicate record of the same
    # person, so erasing the person erases it. Neither form of SET NULL works
    # — the bare one nulls `tenant_id` (NOT NULL), and the Postgres 15+
    # single-column form leaves a merged row with no target, which
    # `ck_candidates_merged_has_target` forbids.
    op.create_foreign_key(
        'fk_candidates_merged_into_same_tenant',
        'candidates',
        'candidates',
        ['tenant_id', 'merged_into_candidate_id'],
        ['tenant_id', 'id'],
        ondelete='CASCADE',
    )
    # Email is matched case-insensitively, so the index must be on lower(email)
    # — a plain index would let Jane@acme.sg and jane@acme.sg both exist and
    # then match the same row unpredictably.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_candidates_tenant_email
        ON candidates (tenant_id, lower(email))
        WHERE email IS NOT NULL AND record_status <> 'merged'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_candidates_tenant_phone
        ON candidates (tenant_id, phone_e164)
        WHERE phone_e164 IS NOT NULL AND record_status <> 'merged'
        """
    )

    op.create_table(
        'candidate_skills',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('candidate_id', sa.UUID(), nullable=False),
        sa.Column('skill', sa.Text(), nullable=False),
        sa.Column('skill_normalized', sa.Text(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'candidate_id'],
            ['candidates.tenant_id', 'candidates.id'],
            name='fk_candidate_skills_candidate_same_tenant',
            ondelete='CASCADE',
        ),
        sa.UniqueConstraint(
            'tenant_id', 'candidate_id', 'skill_normalized',
            name='uq_candidate_skills_once_per_candidate',
        ),
    )
    op.create_index(op.f('ix_candidate_skills_tenant_id'), 'candidate_skills', ['tenant_id'])
    op.create_index(op.f('ix_candidate_skills_candidate_id'), 'candidate_skills', ['candidate_id'])
    op.create_index(
        op.f('ix_candidate_skills_skill_normalized'), 'candidate_skills', ['skill_normalized']
    )

    op.create_table(
        'candidate_field_overrides',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('tenant_id', sa.UUID(), nullable=False),
        sa.Column('candidate_id', sa.UUID(), nullable=False),
        sa.Column('field_name', sa.String(length=64), nullable=False),
        sa.Column('human_value', sa.Text(), nullable=True),
        sa.Column('changed_by', sa.UUID(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(
            ['tenant_id', 'candidate_id'],
            ['candidates.tenant_id', 'candidates.id'],
            name='fk_candidate_overrides_candidate_same_tenant',
            ondelete='CASCADE',
        ),
        sa.UniqueConstraint(
            'tenant_id', 'candidate_id', 'field_name',
            name='uq_candidate_overrides_one_per_field',
        ),
    )
    op.create_index(
        op.f('ix_candidate_field_overrides_tenant_id'), 'candidate_field_overrides', ['tenant_id']
    )
    op.create_index(
        op.f('ix_candidate_field_overrides_candidate_id'),
        'candidate_field_overrides',
        ['candidate_id'],
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
    """FORCE, not merely ENABLE: without it the table owner bypasses the
    policy, and the owner is who migrations and any superuser session connect
    as — so an ENABLE-only table looks protected in the catalogue and is not.
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
    op.drop_table('candidate_field_overrides')
    op.drop_table('candidate_skills')
    op.execute("DROP INDEX IF EXISTS uq_candidates_tenant_phone")
    op.execute("DROP INDEX IF EXISTS uq_candidates_tenant_email")
    op.drop_table('candidates')
