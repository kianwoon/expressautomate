"""tenants.is_personal_account

Personal Microsoft accounts can now sign in. They all share one MSA tenant
GUID, so each such user gets a private tenant derived from their own `oid`
instead. This flag records which kind of tenant a row is, so onboarding and
invites can tell a one-person workspace from a real agency directory without
re-deriving it from `ms_tenant_id`.

Revision ID: 9b3e5d1c8a72
Revises: 5f2c9a3e7b41
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b3e5d1c8a72"
down_revision: str | None = "5f2c9a3e7b41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows predate personal sign-in, so false is the correct backfill;
    # the server_default stays so plain SQL inserts cannot leave it NULL.
    op.add_column(
        "tenants",
        sa.Column(
            "is_personal_account",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "is_personal_account")
