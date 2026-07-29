"""wa_risk_notice_version

Plan §9's onboarding gap: the recruiter must be told, before first pairing,
that connecting WhatsApp risks a Meta ban on their *own* personal number, and
that acknowledgement must be recorded.

`wa_sessions.ban_risk_acknowledged_at` already exists
(`20260729_2100_wa_gateway_sessions.py`) but was never wired to a route, and
by itself it is nearly worthless — a timestamp says *that* someone clicked
something, never *what they were shown*. This revision adds
`risk_notice_version`, the version (a content hash — see
`app/services/wa_risk_notice.py`) of the notice text acknowledged at that
timestamp, so "what was this recruiter told?" has a reproducible answer
instead of a rubber stamp.

Both columns stay nullable. An existing session predates this feature and has
genuinely acknowledged nothing — backfilling either column would record a
consent nobody gave.

Revision ID: e1a9c5f3d7b2
Revises: d8e51f0a2b6c
Create Date: 2026-07-29 23:59:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1a9c5f3d7b2"
down_revision: str | None = "d8e51f0a2b6c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "wa_sessions",
        sa.Column("risk_notice_version", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wa_sessions", "risk_notice_version")
