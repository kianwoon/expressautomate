"""owner role

Revision ID: f1c40a9d5e72
Revises: e5b92d8a7c41
Create Date: 2026-07-28 15:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f1c40a9d5e72'
down_revision: str | None = 'e5b92d8a7c41'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Backfill before constraining: a tenant whose users all predate this
    # change has no owner, and its personal-data deletion would be
    # unreachable — the exact failure this task exists to prevent.
    op.execute(
        """
        UPDATE users SET role = 'owner'
        WHERE id IN (
            SELECT DISTINCT ON (tenant_id) id
            FROM users
            ORDER BY tenant_id, created_at ASC, id ASC
        )
        """
    )
    # Any value that is neither is a typo. Left unconstrained it would deny
    # access silently, which reads as "the button is broken".
    op.execute("UPDATE users SET role = 'recruiter' WHERE role NOT IN ('owner', 'recruiter')")
    op.create_check_constraint(
        'ck_users_role', 'users', "role IN ('owner', 'recruiter')"
    )
    # Created AFTER the backfill, which has just guaranteed exactly one owner
    # per tenant. Enforcing it in the schema means a future code path that
    # forgets to check cannot quietly produce a second owner — and the only
    # thing standing between "two owners" and "nobody notices" is this index.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_users_one_owner_per_tenant
        ON users (tenant_id) WHERE role = 'owner'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_users_one_owner_per_tenant")
    op.drop_constraint('ck_users_role', 'users', type_='check')
