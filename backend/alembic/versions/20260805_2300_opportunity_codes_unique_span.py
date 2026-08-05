"""unique constraint on opportunity_codes span

Revision ID: c1a0d5e7b215
Revises: c1a0d5e7b214
Create Date: 2026-08-05 23:00:00+00:00

Adds `UNIQUE (opportunity_id, code, start_char, end_char)` to
`opportunity_codes`, so re-extracting an email cannot insert the same code at
the same offset a second time.

The opportunity insert has guarded against this from the start with
`ON CONFLICT (id) DO NOTHING`, because a retried extraction reproduces the same
opportunity id. The code insert never had the matching guard: `_INSERT_CODE`
minted a fresh UUID every time, and nothing stopped a second extraction's
`detect()` — which is deterministic, so it finds the same spans — from writing
a second row for each code. The duplicate was invisible to the eye (two
identical lines) but real in the data, and would accumulate one row per code
per re-extraction.

The constraint key is `(opportunity_id, code, start_char, end_char)`: the
natural identity of one detected occurrence. A code genuinely appearing twice
in one email lands at two different offsets, so those are two distinct keys and
both survive — this guards only the exact-replay case, not legitimate repeats.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1a0d5e7b215"
down_revision: str | None = "c1a0d5e7b214"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Remove any rows that already violate the constraint before adding it: a
    # unique constraint cannot be created in the presence of duplicates. Keeps
    # the earliest row of each duplicate set (MIN(id)) and drops the rest, so
    # the first-recorded occurrence is the one that stays.
    op.execute(
        """
        DELETE FROM opportunity_codes a
         USING opportunity_codes b
         WHERE a.opportunity_id = b.opportunity_id
           AND a.code = b.code
           AND a.start_char = b.start_char
           AND a.end_char = b.end_char
           AND a.id > b.id
        """
    )
    op.create_unique_constraint(
        "uq_opportunity_codes_once_per_span",
        "opportunity_codes",
        ["opportunity_id", "code", "start_char", "end_char"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_opportunity_codes_once_per_span", "opportunity_codes", type_="unique")
