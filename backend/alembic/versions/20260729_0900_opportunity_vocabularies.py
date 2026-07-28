"""opportunity vocabularies

Pins `salary_period` and `quality_state` to the words their readers understand.

Both were bare String(16) columns whose vocabulary was guaranteed only by the
function that wrote them. `quality_state` held up; `salary_period` did not — it
went in verbatim from the extraction, so "Month" reached the column and the
salary sort dropped those rows to the bottom, having looked the period up by
the canonical word.

The normalising UPDATE runs first, so the constraint cannot fail on data that
predates it. Against the production database it is a no-op — `opportunities`
was empty when this was written, ingestion not having started — but a
development or staging database filled from real emails is exactly where an
unnormalised row lives, and a migration that fails there is a migration nobody
can run.

Revision ID: 3f9a6c21e8b4
Revises: b7c1e4a2d905
Create Date: 2026-07-29 09:00:00.000000+00:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = '3f9a6c21e8b4'
down_revision: str | None = 'b7c1e4a2d905'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors `_SALARY_PERIODS` in app/services/ingest/persist.py. Written out
# rather than imported: a migration has to keep meaning what it meant on the
# day it ran, and importing the live vocabulary would let a later edit rewrite
# the history of this one.
_PERIODS = ("hour", "day", "week", "month", "year")
_QUALITY_STATES = ("needs_review", "likely", "verified")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # Case and padding first: "Month" and " month " are the period they name,
    # and refusing them here would throw away a figure the sort can rank.
    op.execute("UPDATE opportunities SET salary_period = lower(btrim(salary_period))")
    # Anything still outside the vocabulary was never readable — the sort
    # already treated it as missing, so NULL states what was already true
    # rather than guessing which period was meant.
    op.execute(
        f"UPDATE opportunities SET salary_period = NULL "
        f"WHERE salary_period IS NOT NULL AND salary_period NOT IN ({_in_list(_PERIODS)})"
    )
    op.create_check_constraint(
        "ck_opportunities_salary_period_known",
        "opportunities",
        f"salary_period IS NULL OR salary_period IN ({_in_list(_PERIODS)})",
    )
    # No matching UPDATE: `quality_state` is NOT NULL and has only ever been
    # written by `quality_state()`, so there is nothing to repair. A row that
    # somehow held anything else would fail here, which is the right outcome —
    # silently rewriting a quality judgement is worse than a refused migration.
    op.create_check_constraint(
        "ck_opportunities_quality_state_known",
        "opportunities",
        f"quality_state IN ({_in_list(_QUALITY_STATES)})",
    )


def downgrade() -> None:
    # Only the constraints come off. The normalised values stay: they are the
    # same periods spelled the way every reader expects, and un-lowercasing
    # them would need a record of the original casing that was never kept.
    op.drop_constraint("ck_opportunities_quality_state_known", "opportunities")
    op.drop_constraint("ck_opportunities_salary_period_known", "opportunities")
