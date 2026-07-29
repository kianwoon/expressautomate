"""candidate regulatory facts, and the languages they speak

Five nullable columns on `candidates` and one new table. Every one of them is a
FACT recorded because Singapore law asks for it on a form — MOM's Work Permit
conditions for a migrant domestic worker turn on sex, age and years of formal
education, statutory CDAC/MBMF/SINDA/ECF deductions turn on the CMIO race
group, and work-pass eligibility turns on nationality.

None of them is a selection criterion, and this revision adds no index that
would make one convenient. `app/services/sourcing/redact.py` is the statement
of why: the platform refuses to rank people on protected characteristics, and
storing a fact a form needs must not become the thing that launders a filter.

Every column is nullable and NULL means "not recorded". There is no backfill,
no default and no inference — a name is not evidence of sex (§15).

The RLS policy for `candidate_languages` is created in this same revision as
the table, not a later one: `verify_rls_enforced()` (`app/db/rls.py:58`)
refuses to boot on any readable table without a forced policy, so splitting
them is a failed deploy rather than a silent cross-tenant leak.
"""

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "a4d8f6b2c103"
down_revision: str | None = "f2b6a1d9c4e8"
branch_labels = None
depends_on = None

_SETTING = "app.tenant_id"

# Written out rather than imported from the model. A migration is a record of
# what was applied on the day it ran, and a model constant that changes later
# would silently rewrite history — the same reason the other vocabulary
# migrations in this directory spell their values out.
_CANDIDATE_CHECKS = (
    ("ck_candidates_sex", "sex IS NULL OR sex IN ('female','male')"),
    ("ck_candidates_race", "race IS NULL OR race IN ('chinese','malay','indian','others')"),
    (
        "ck_candidates_nationality_iso_alpha2",
        "nationality IS NULL OR nationality ~ '^[A-Z]{2}$'",
    ),
    (
        "ck_candidates_education_years_range",
        "education_years IS NULL OR (education_years >= 0 AND education_years <= 30)",
    ),
)


def upgrade() -> None:
    op.add_column("candidates", sa.Column("sex", sa.String(16)))
    op.add_column("candidates", sa.Column("race", sa.String(16)))
    op.add_column("candidates", sa.Column("race_detail", sa.Text()))
    # ISO 3166-1 alpha-2 — see the column comment in `app/models/candidate.py`.
    op.add_column("candidates", sa.Column("nationality", sa.String(2)))
    # A date, never an age.
    op.add_column("candidates", sa.Column("date_of_birth", sa.Date()))
    op.add_column("candidates", sa.Column("education_years", sa.Integer()))

    for name, expression in _CANDIDATE_CHECKS:
        op.create_check_constraint(name, "candidates", expression)

    op.create_table(
        "candidate_languages",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("language_normalized", sa.Text(), nullable=False),
        sa.Column("fluency", sa.String(16)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        # Composite, carrying tenant_id: RLS filters what a statement may read
        # and write, but not the referential-integrity check behind a plain FK,
        # so a plain one would let agency A's language row name agency B's
        # candidate (§18, and `tests/test_candidate_isolation.py`).
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_languages_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "language_normalized",
            name="uq_candidate_languages_once_per_candidate",
        ),
        sa.CheckConstraint(
            "fluency IS NULL OR fluency IN ('native','fluent','conversational','basic')",
            name="ck_candidate_languages_fluency",
        ),
    )
    op.create_index("ix_candidate_languages_tenant_id", "candidate_languages", ["tenant_id"])
    op.create_index(
        "ix_candidate_languages_candidate_id", "candidate_languages", ["candidate_id"]
    )
    op.create_index(
        "ix_candidate_languages_language_normalized",
        "candidate_languages",
        ["language_normalized"],
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON candidate_languages "
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )

    predicate = f"tenant_id = nullif(current_setting('{_SETTING}', true), '')::uuid"
    op.execute("ALTER TABLE candidate_languages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE candidate_languages FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_languages")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON candidate_languages
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON candidate_languages")
    op.drop_table("candidate_languages")

    for name, _expression in _CANDIDATE_CHECKS:
        op.drop_constraint(name, "candidates", type_="check")

    for column in (
        "education_years",
        "date_of_birth",
        "nationality",
        "race_detail",
        "race",
        "sex",
    ):
        op.drop_column("candidates", column)
