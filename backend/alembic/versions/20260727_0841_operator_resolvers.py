"""operator resolvers

The periodic sweeps in `app/workers/tasks.py` run across every tenant at once,
so they have no single tenant context to set. The alternative to these
functions is a role that bypasses RLS, which would be a far larger exemption
than a handful of fixed queries returning routing columns.

Each is `SECURITY DEFINER` with a pinned `search_path`, mirroring
`resolve_subscription`. None of them returns message content — only the ids a
sweep needs to enqueue work, which the job then re-reads under its own tenant
policy.

Revision ID: 0c95f92ac6ad
Revises: 794108192476
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "0c95f92ac6ad"
down_revision: str | None = "794108192476"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (signature, argument types for GRANT/DROP, body)
FUNCTIONS: list[tuple[str, str, str]] = [
    (
        "stalled_email_rows(p_pending_minutes int, p_working_minutes int)",
        "int, int",
        """
        RETURNS TABLE (id uuid, tenant_id uuid, mailbox_id uuid,
                       processing_status text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            -- tenant_id and mailbox_id are returned because every job carries
            -- its tenant. Without them the sweep would enqueue jobs that raise
            -- TypeError inside the worker, and the recovery net would be doing
            -- nothing at all while appearing to run.
            --
            -- Two grace periods: a queue hop should be quick, but a fetch or an
            -- extraction legitimately takes longer, so they are not swept on
            -- the same clock. Terminal statuses are absent on purpose.
            SELECT e.id, e.tenant_id, e.mailbox_id, e.processing_status
            FROM email_messages e
            WHERE (e.processing_status = 'pending'
                   AND e.updated_at < now()
                       - make_interval(mins => p_pending_minutes))
               OR (e.processing_status IN ('fetched', 'classifying', 'extracting')
                   AND e.updated_at < now()
                       - make_interval(mins => p_working_minutes))
        $$
        """,
    ),
    (
        "subscriptions_due_for_renewal(p_margin double precision)",
        "double precision",
        """
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid, subscription_id text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            -- The window is measured from the start of the CURRENT lifetime,
            -- so a renewed subscription counts from `last_renewed_at`. Using
            -- `created_at` forever would renew ever more often as the row aged.
            --
            -- The mailbox must be active too. A subscription on a mailbox
            -- whose grant is dead can never renew — the sweep would raise
            -- MailboxNotAuthorised, log a warning and flip an already-flipped
            -- status every fifteen minutes until the subscription expired.
            SELECT s.tenant_id, s.mailbox_id, s.subscription_id
            FROM graph_subscriptions s
            JOIN mailboxes m ON m.id = s.mailbox_id AND m.status = 'active'
            WHERE s.status = 'active'
              AND now() >= coalesce(s.last_renewed_at, s.created_at)
                  + (s.expires_at - coalesce(s.last_renewed_at, s.created_at))
                    * p_margin
        $$
        """,
    ),
    (
        "mailboxes_without_subscription()",
        "",
        """
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            -- An active mailbox with no ACTIVE subscription receives nothing
            -- and looks perfectly healthy. Nothing else finds this state:
            -- `subscriptions_due_for_renewal` only scans rows that exist, and
            -- `active_mailboxes` does not care whether one does.
            --
            -- Two ways in. A `recreate_subscription` whose create failed after
            -- the retire committed, and a mailbox reconnected after
            -- `needs_reauth` — its old subscription was retired and nothing
            -- ever made a new one.
            SELECT m.tenant_id, m.id
            FROM mailboxes m
            WHERE m.status = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM graph_subscriptions s
                  WHERE s.mailbox_id = m.id AND s.status = 'active'
              )
        $$
        """,
    ),
    (
        "active_mailboxes()",
        "",
        """
        RETURNS TABLE (tenant_id uuid, mailbox_id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            -- Only `active`. Sweeping a mailbox whose grant is dead burns a
            -- token refresh and logs the same failure every ten minutes.
            SELECT m.tenant_id, m.id FROM mailboxes m WHERE m.status = 'active'
        $$
        """,
    ),
]


def upgrade() -> None:
    role = settings.DATABASE_APP_ROLE
    for signature, arg_types, body in FUNCTIONS:
        name = signature.split("(", 1)[0]
        op.execute(f"CREATE OR REPLACE FUNCTION {signature} {body}")
        # Postgres grants EXECUTE to PUBLIC by default, which would hand these
        # RLS-bypassing functions to every role that can connect.
        op.execute(f"REVOKE ALL ON FUNCTION {name}({arg_types}) FROM PUBLIC")
        op.execute(f'GRANT EXECUTE ON FUNCTION {name}({arg_types}) TO "{role}"')


def downgrade() -> None:
    for signature, arg_types, _ in FUNCTIONS:
        name = signature.split("(", 1)[0]
        op.execute(f"DROP FUNCTION IF EXISTS {name}({arg_types})")
