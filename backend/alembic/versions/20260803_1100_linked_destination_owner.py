"""a paired WhatsApp destination must have an owner

One CHECK constraint on `notification_destinations`: a `whatsapp_linked` row
may not have a null `user_id`.

The ownership rule itself lives in the API (`_OWN_DEVICE_ONLY` in
app/api/notifications.py) — a personal handset is visible and editable only to
the recruiter who paired it. This is the database's half of it. For every other
channel `user_id IS NULL` means "the agency's shared feed", and that reading
cannot apply to a device: the delivery job opens the Baileys socket belonging
to `user_id`, and the settings API shows the row to that user alone. A null
owner would be a destination nobody can see and nothing can send, so it is
made unrepresentable rather than left to each write path to avoid.

Nothing is backfilled and nothing should need to be: the only writer,
`create_destination` via the whatsapp-linked endpoint, always passes the
caller's id, and `set_scope` refuses to promote a linked row. If the constraint
fails to validate on deploy, that is the discovery that some path did null one
out — which is the point of adding it.

Revision ID: c1a0d5e7b209
Revises: c1a0d5e7b208
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
from app.models.notification import CHANNEL_WHATSAPP_LINKED

revision: str = "c1a0d5e7b209"
down_revision: str | None = "c1a0d5e7b208"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ck_destination_linked_has_owner"


def upgrade() -> None:
    op.create_check_constraint(
        _NAME,
        "notification_destinations",
        f"channel <> '{CHANNEL_WHATSAPP_LINKED}' OR user_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_NAME, "notification_destinations", type_="check")
