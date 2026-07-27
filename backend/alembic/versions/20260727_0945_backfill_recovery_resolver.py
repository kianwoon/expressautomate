"""backfill recovery resolver

`enqueue` never raises — a failed enqueue leaves durable work with no job, and
the recovery sweeps are what turn that back into queued work. Subscriptions
already had `mailboxes_without_subscription`; the historical backfill had
nothing.

That gap is quiet in the worst way. `ensure_subscriptions` restores the
subscription, so new mail flows and the mailbox looks entirely healthy, while
the history the user connected the mailbox *for* is never walked. Nothing else
finds it: `rescan_stuck` only knows about `email_messages` rows.

Revision ID: 6917f8d2035a
Revises: 0c95f92ac6ad
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "6917f8d2035a"
down_revision: str | None = "0c95f92ac6ad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "mailboxes_awaiting_backfill"


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mailboxes_awaiting_backfill()
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            -- `backfill_completed_at` is set by `backfill_mailbox` when the
            -- walk finishes, including when the message cap stopped it early
            -- (spec: whichever limit is hit first marks the mailbox backfilled
            -- from that point). NULL therefore means the walk never ran.
            --
            -- Active only: walking needs a working grant, so a mailbox in
            -- `needs_reauth` would fail this every hour.
            SELECT m.tenant_id, m.id
            FROM mailboxes m
            WHERE m.status = 'active' AND m.backfill_completed_at IS NULL
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {NAME}() FROM PUBLIC")
    op.execute(f'GRANT EXECUTE ON FUNCTION {NAME}() TO "{settings.DATABASE_APP_ROLE}"')


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {NAME}()")
