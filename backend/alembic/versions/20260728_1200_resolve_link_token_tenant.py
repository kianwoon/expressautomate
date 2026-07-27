"""resolve_link_token_tenant

Task 10: the Telegram webhook. `/start <token>` arrives with no session and
no tenant — Telegram carries no cookie — so something has to map the token to
its owning tenant *before* `tenant_session` can even be opened. Every other
pre-tenant lookup in this codebase (`resolve_subscription`,
`claim_fetched_email_rows`, `flush_notification_deliveries`) solves the same
shape of problem with a narrow `SECURITY DEFINER` function rather than a
second RLS policy that reads unscoped, and this follows that pattern rather
than adding one to `notification_link_tokens`.

A permissive SELECT policy would be strictly worse here: it would stay live
for *any* unscoped connection for as long as the table exists, not just for
this one lookup, and it separates "who can read this row" from the query the
one legitimate caller actually needs — a token hash in, a tenant id out. The
function keeps that boundary explicit: it returns tenant_id and nothing
else, so no user id, address, or channel ever crosses out of it. The caller
re-reads everything else (via `redeem_token`) under the tenant's own policy
once `tenant_session` is open.

The predicate mirrors `redeem_token`'s: unconsumed and unexpired, so a spent
or expired token resolves to no tenant at all, exactly as it resolves to no
row for `redeem_token` itself.

Revision ID: c2f9a4d17b06
Revises: b1a1000a061c
Create Date: 2026-07-28
"""

from collections.abc import Sequence

from alembic import op
from app.core.config import settings

revision: str = "c2f9a4d17b06"
down_revision: str | None = "b1a1000a061c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "resolve_link_token_tenant(p_token_hash text, p_channel text)"
ARG_TYPES = "text, text"
BODY = """
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- Only tenant_id crosses this boundary. Everything else about the
    -- token — user_id, address, channel — is re-read by redeem_token once
    -- the caller has a tenant-scoped session, under the ordinary policy.
    SELECT tenant_id FROM notification_link_tokens
    WHERE token_hash = p_token_hash
      AND channel = p_channel
      AND consumed_at IS NULL
      AND expires_at > now()
$$
"""


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {BODY}")
    # Postgres grants EXECUTE to PUBLIC by default, which would hand an
    # RLS-bypassing function to every role that can connect.
    op.execute(
        f"REVOKE ALL ON FUNCTION resolve_link_token_tenant({ARG_TYPES}) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' resolve_link_token_tenant({ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS resolve_link_token_tenant({ARG_TYPES})")
