"""client state not empty

A subscription's `client_state` is the only thing standing between a public,
unauthenticated webhook URL and a forged payload. `_client_state_matches` in
`app/api/graph_webhook.py` already refuses an empty one, and that is the
enforcement point — but this is the layer that decides what a row may contain:
a subscription carrying an empty secret should be impossible to create, not
merely harmless to receive.

Carried forward from the Task 5 review.

Revision ID: 794108192476
Revises: f1efd83cfaba
Create Date: 2026-07-27 08:16:36.801042+00:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = '794108192476'
down_revision: str | None = 'f1efd83cfaba'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_graph_subscriptions_client_state_not_empty"


def upgrade() -> None:
    op.create_check_constraint(CONSTRAINT, "graph_subscriptions", "client_state <> ''")


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "graph_subscriptions", type_="check")
