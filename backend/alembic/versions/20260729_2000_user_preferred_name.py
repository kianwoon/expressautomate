"""user preferred name

A recruiter's name reaches a candidate in the WhatsApp outreach draft, and
`display_name` cannot be the editable field: sign-in overwrites it from the
Entra/Google claims on every login (see the upsert in app/api/auth.py), so an
edit there would be silently reverted at next sign-in. `preferred_name` is a
second column written only by `PATCH /api/auth/me`, never by sign-in — the
same separation `candidate_field_overrides` already draws between an
imported fact and a person's decision.

Revision ID: 6f1a9c3d0b52
Revises: 5c2d8e14f6a1
Create Date: 2026-07-29 20:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f1a9c3d0b52"
down_revision: str | None = "5c2d8e14f6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("preferred_name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "preferred_name")
