"""sourcing run sex prefilter columns

Revision ID: c1a0d5e7b213
Revises: c1a0d5e7b212
Create Date: 2026-08-05 21:00:00+00:00

Adds `sex_prefilter_applied` and `sex_prefilter_value` to `sourcing_runs`, so
the row records — honestly and queryably — when a run's candidate pool was
narrowed to a single sex because the client's email shorthand (C/F, O/F, ...)
implied one.

These columns exist to keep a record that previously said one thing honest
after it starts doing another. `protected_attribute_noticed` and its note
describe what the explanation model *noticed* in the job text; a run that goes
on to *act* on a coded sex preference (by dropping candidates of the other sex
from the pool before scoring) needs its own flag, or the `Safeguards` banner's
"the shortlist ignored that requirement" wording becomes a lie. Recruiters and
any future auditor must be able to read off the row what actually happened, not
infer it.

This is a deliberate product decision to honour a client's stated sex preference
in sourcing. The legal exposure is real (Singapore Tripartite Guidelines on
Fair Employment Practices); the mitigation is the audit trail these columns are
part of, plus keeping the action in sourcing rather than recording it as a
false occupational `sex_requirement` on the job order.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c1a0d5e7b213"
down_revision: str | None = "c1a0d5e7b212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sourcing_runs",
        sa.Column(
            "sex_prefilter_applied",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "sourcing_runs",
        sa.Column("sex_prefilter_value", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_sourcing_runs_sex_prefilter_value_when_applied",
        "sourcing_runs",
        "(sex_prefilter_applied) = (sex_prefilter_value IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_sourcing_runs_sex_prefilter_value_known",
        "sourcing_runs",
        "sex_prefilter_value IS NULL OR sex_prefilter_value IN ('female', 'male')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_sourcing_runs_sex_prefilter_value_known", "sourcing_runs", type_="check"
    )
    op.drop_constraint(
        "ck_sourcing_runs_sex_prefilter_value_when_applied",
        "sourcing_runs",
        type_="check",
    )
    op.drop_column("sourcing_runs", "sex_prefilter_value")
    op.drop_column("sourcing_runs", "sex_prefilter_applied")
