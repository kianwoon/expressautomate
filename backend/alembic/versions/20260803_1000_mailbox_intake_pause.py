"""mailbox intake pause

One nullable column on `mailboxes`: `ingest_paused_at`. NULL means intake is
running; a timestamp means the owner switched it off, and records since when —
the UI shows that, because the failure mode of this feature is forgetting it
is off (see docs/superpowers/specs/2026-08-03-mailbox-intake-pause-design.md).

`active_mailboxes()` is redefined to exclude paused mailboxes, so the delta
sweep does not queue work the job gate would immediately discard.
`subscriptions_due_for_renewal()` is deliberately **left alone**: a paused
mailbox keeps its Graph subscription current, so resume does not have to
recreate it — the subscription lapsing mid-vacation is exactly the failure a
`status = 'paused'` enum value would have caused, and why this is a separate
column instead.

The redefinition repeats `SECURITY DEFINER` and `SET search_path` in full.
`CREATE OR REPLACE` silently reverts a function to SECURITY INVOKER when they
are omitted, and the app role runs under FORCE ROW LEVEL SECURITY — the
resolver would then see only one tenant's mailboxes and the sweep would
quietly stop working for everyone else.

Revision ID: c1a0d5e7b208
Revises: c1a0d5e7b207
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "c1a0d5e7b208"
down_revision: str | None = "c1a0d5e7b207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The new body. Identical to the one in 20260727_0841 except for the pause
# predicate — including the SECURITY DEFINER / search_path pair, which must
# travel with every redefinition or the function loses them.
_PAUSED_AWARE = """
CREATE OR REPLACE FUNCTION active_mailboxes()
RETURNS TABLE (tenant_id uuid, mailbox_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- Only `active`. Sweeping a mailbox whose grant is dead burns a
    -- token refresh and logs the same failure every ten minutes.
    --
    -- And only unpaused. This is belt-and-braces — the job entry points
    -- (`fetch_email`, `delta_sync_mailbox`) are the authoritative gate —
    -- but a sweep that queues work only for it to be discarded is noise
    -- in the queue and in the logs.
    SELECT m.tenant_id, m.id FROM mailboxes m
    WHERE m.status = 'active' AND m.ingest_paused_at IS NULL
$$
"""

# The previous body, verbatim from 20260727_0841_operator_resolvers.py, so a
# downgrade restores exactly what that migration created.
_ORIGINAL = """
CREATE OR REPLACE FUNCTION active_mailboxes()
RETURNS TABLE (tenant_id uuid, mailbox_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- Only `active`. Sweeping a mailbox whose grant is dead burns a
    -- token refresh and logs the same failure every ten minutes.
    SELECT m.tenant_id, m.id FROM mailboxes m WHERE m.status = 'active'
$$
"""


def _lock_down() -> None:
    """Re-apply the grant discipline from 20260727_0841.

    `CREATE OR REPLACE` preserves existing privileges, so repeating these is
    defence in depth rather than a fix — but a migration that *depends* on
    that preservation would break silently the day someone rewrites it as a
    DROP + CREATE, so the lock-down travels with every redefinition.
    """
    role = settings.DATABASE_APP_ROLE
    op.execute("REVOKE ALL ON FUNCTION active_mailboxes() FROM PUBLIC")
    op.execute(f'GRANT EXECUTE ON FUNCTION active_mailboxes() TO "{role}"')


def upgrade() -> None:
    op.add_column(
        "mailboxes",
        sa.Column("ingest_paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(_PAUSED_AWARE)
    _lock_down()


def downgrade() -> None:
    op.execute(_ORIGINAL)
    _lock_down()
    op.drop_column("mailboxes", "ingest_paused_at")
