"""backfill sweep skips what it cannot walk

Found by watching production: `ensure_backfills` selects active mailboxes with
`backfill_completed_at IS NULL`, but `backfill_mailbox_job` returns early —
without setting that column — when the mailbox has no `initial_sync_from` or no
owner to borrow a token from. Two such rows existed, and the sweep would have
re-enqueued them every hour forever: harmless work, endless noise, and a log
line that looks like a failing backfill when nothing is wrong.

The sweep now asks only for mailboxes the job can actually walk. A mailbox with
no owner is a separate problem, surfaced by `mailbox_needs_reauth` rather than
retried hourly.

Revision ID: 498eaa862e4f
Revises: 6917f8d2035a
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "498eaa862e4f"
down_revision: str | None = "6917f8d2035a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mailboxes_awaiting_backfill()
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT m.tenant_id, m.id
            FROM mailboxes m
            WHERE m.status = 'active'
              AND m.backfill_completed_at IS NULL
              -- Only what the job can act on. Without a start date there is no
              -- window to walk, and without an owner there is no grant to walk
              -- it with; in both cases the job returns early and leaves
              -- `backfill_completed_at` NULL, so an unfiltered sweep would
              -- re-enqueue the same rows on every tick, forever.
              AND m.initial_sync_from IS NOT NULL
              AND m.user_id IS NOT NULL
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION mailboxes_awaiting_backfill() FROM PUBLIC")
    op.execute(
        'GRANT EXECUTE ON FUNCTION mailboxes_awaiting_backfill() '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION mailboxes_awaiting_backfill()
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT m.tenant_id, m.id
            FROM mailboxes m
            WHERE m.status = 'active' AND m.backfill_completed_at IS NULL
        $$
        """
    )
