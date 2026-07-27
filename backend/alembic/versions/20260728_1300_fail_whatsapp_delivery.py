"""fail_whatsapp_delivery

Task 11: the Meta WhatsApp webhook. A delivery-status callback names a
`provider_message_id` and no tenant — Meta has no notion of our tenants, and
the id is the only handle the callback carries — so the update that marks
that one delivery `failed` cannot run inside `tenant_session`. The brief this
task was cut from proposed a second, permissive RLS policy on
`notification_deliveries`:

    CREATE POLICY provider_callback ON notification_deliveries
      FOR ALL USING (nullif(current_setting('app.tenant_id', true), '') IS NULL)

That is wrong for the same reason the same idea was wrong for
`notification_link_tokens` (see `resolve_link_token_tenant`): a `FOR ALL`
policy stays live for *any* unscoped connection for as long as the table
exists, not just for this one callback, and `notification_deliveries` is a
far more sensitive table than the link-token one — its rows carry
`error` text and every tenant's delivery history. Every other unscoped
pre-tenant read or write in this codebase (`flush_notification_deliveries`,
`resolve_link_token_tenant`, `claim_fetched_email_rows`) solves the same
shape of problem with a narrow `SECURITY DEFINER` function instead, and this
follows that pattern.

The function touches exactly the one delivery row matching
`p_provider_message_id` — never a wildcard or a broader predicate — and, in
the same statement, disables the one destination that row points at: a
`notification_destinations` write is exactly as unreachable from an unscoped
connection as the deliveries write is, so doing it as a second, separately
unscoped `UPDATE` in the webhook (as an earlier version of this function
did) hit the same RLS wall one query later. Chaining both writes through one
CTE keeps them atomic and keeps the "touches exactly one row" guarantee for
both tables — the destination update is keyed off `destination_id` returned
by the delivery update, never off caller input. The function returns only
`destination_id` and `tenant_id`, the routing ids the webhook logs; it never
returns `error`, `subject_id`, or any other column, so no delivery content
crosses out of RLS through this path.

`whatsapp_suppressions` needs no equivalent: it already carries a permissive
`global_read` policy (`USING (true) WITH CHECK (true)`, from the
`notifications` migration) by design — a suppression is deliberately global,
not tenant data — so an ordinary unscoped `INSERT` already succeeds under
its own policy. Adding a function there would just be an extra hop to the
same place.

Revision ID: d3a0b5e28c17
Revises: c2f9a4d17b06
Create Date: 2026-07-28
"""

from collections.abc import Sequence

from alembic import op
from app.core.config import settings

revision: str = "d3a0b5e28c17"
down_revision: str | None = "c2f9a4d17b06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "fail_whatsapp_delivery(p_provider_message_id text, p_error text)"
ARG_TYPES = "text, text"
BODY = """
RETURNS TABLE (destination_id uuid, tenant_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- Scoped to exactly one row by the provider's own message id, which is
    -- ours — it is the id our send path received back from Meta and stored
    -- on this exact row, not a caller-supplied filter that could widen. Only
    -- routing ids cross this boundary: no error text, subject id, or address
    -- ever leaves through this function, even though the first UPDATE writes
    -- `error`.
    WITH failed AS (
        UPDATE notification_deliveries
        SET status = 'failed', error = p_error
        WHERE provider_message_id = p_provider_message_id
        RETURNING destination_id, tenant_id
    ),
    -- Keyed off `failed.destination_id`, not off any caller-supplied id, so
    -- this can only ever touch the destination the matched delivery itself
    -- points at.
    disabled AS (
        UPDATE notification_destinations d
        SET disabled_at = now()
        FROM failed
        WHERE d.id = failed.destination_id
    )
    SELECT destination_id, tenant_id FROM failed
$$
"""


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {BODY}")
    # Postgres grants EXECUTE to PUBLIC by default, which would hand an
    # RLS-bypassing function to every role that can connect.
    op.execute(f"REVOKE ALL ON FUNCTION fail_whatsapp_delivery({ARG_TYPES}) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' fail_whatsapp_delivery({ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS fail_whatsapp_delivery({ARG_TYPES})")
