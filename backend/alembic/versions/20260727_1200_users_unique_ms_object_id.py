"""users unique ms_object_id

A user's identity is the Entra object id, not the mail address. The UPN behind
`preferred_username` is mutable: a rename would otherwise insert a second row
(orphaning that user's ms_oauth_tokens), and an address reassigned to a new hire
would silently hand them the previous holder's stored refresh token.

Revision ID: 5f2c9a3e7b41
Revises: c4a1d7f9b210
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5f2c9a3e7b41"
down_revision: str | None = "c4a1d7f9b210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "uq_users_tenant_ms_object_id"


def upgrade() -> None:
    # Nullable ms_object_id is fine: Postgres treats NULLs as distinct, so
    # users with no Entra link (none yet, but the column allows it) do not clash.
    op.create_unique_constraint(CONSTRAINT, "users", ["tenant_id", "ms_object_id"])


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "users", type_="unique")
