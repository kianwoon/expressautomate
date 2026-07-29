"""placement type and genuine occupational sex requirement on opportunities

Two facts about a vacancy, kept apart from each other and from anything
regulatory eligibility computes.

`placement_type` says what kind of placement this vacancy is — local hire, a
migrant domestic worker's Work Permit, another Work Permit, S Pass or
Employment Pass. It is set by a person, never inferred, and in particular
never derived from the `EP`/`SP`/`WP` glossary codes: those are marked
`attribute="nationality"` in `app/api/glossary.py` and are redacted from
extracted text before a model reads it (`app/services/sourcing/redact.py`).
Deriving a regulatory fact from the very text redaction exists to scrub would
re-import through the back door what that module removes at the front.

`sex_requirement`/`sex_requirement_reason` record a genuine occupational
requirement the job itself imposes, plus the recruiter's own words for why.
Singapore permits a sex requirement only where there is a genuine practical
reason, so an unexplained one is exactly what the law does not allow — the
pairing CHECK below refuses that shape at the database, and
`app/api/opportunities.py` refuses it at the API. Distinct from the coded
client preference `C/F` decodes to (recorded as evidence, never acted on) and
from `Candidate.sex` (a fact about a person).
"""

import sqlalchemy as sa
from alembic import op

revision: str = "2fb4171f2a02"
down_revision: str | None = "a4d8f6b2c103"
branch_labels = None
depends_on = None

# Written out rather than imported from the model — a migration is a record
# of what was applied on the day it ran, the pattern every other vocabulary
# migration in this directory follows.
_PLACEMENT_TYPE_CHECK = (
    "ck_opportunities_placement_type_known",
    "placement_type IS NULL OR placement_type IN "
    "('local_hire', 'mdw_work_permit', 'other_work_permit', "
    "'s_pass', 'employment_pass')",
)
_SEX_REQUIREMENT_CHECK = (
    "ck_opportunities_sex_requirement_known",
    "sex_requirement IS NULL OR sex_requirement IN ('female', 'male')",
)
_SEX_REQUIREMENT_PAIR_CHECK = (
    "ck_opportunities_sex_requirement_has_reason",
    "(sex_requirement IS NULL) = (sex_requirement_reason IS NULL)",
)


def upgrade() -> None:
    op.add_column("opportunities", sa.Column("placement_type", sa.String(32)))
    op.add_column("opportunities", sa.Column("sex_requirement", sa.String(16)))
    op.add_column("opportunities", sa.Column("sex_requirement_reason", sa.Text()))

    for name, expression in (
        _PLACEMENT_TYPE_CHECK,
        _SEX_REQUIREMENT_CHECK,
        _SEX_REQUIREMENT_PAIR_CHECK,
    ):
        op.create_check_constraint(name, "opportunities", expression)


def downgrade() -> None:
    for name, _expression in (
        _PLACEMENT_TYPE_CHECK,
        _SEX_REQUIREMENT_CHECK,
        _SEX_REQUIREMENT_PAIR_CHECK,
    ):
        op.drop_constraint(name, "opportunities", type_="check")

    op.drop_column("opportunities", "sex_requirement_reason")
    op.drop_column("opportunities", "sex_requirement")
    op.drop_column("opportunities", "placement_type")
