"""claim_fetched_email_rows

The relevance gate runs in batches now, so the thing that starts it is a sweep
across every tenant rather than a job enqueued per email. Like the other
sweeps it therefore has no tenant context, and like them it goes through a
narrow `SECURITY DEFINER` function instead of a role that bypasses RLS.

This one writes, which the earlier resolvers do not. Claiming and reading have
to be the same statement: a sweep that only read `fetched` rows would hand the
same emails to a second batch on the next tick — every one of them paid for
twice at the model — because the rows stay `fetched` until the job that was
enqueued for them actually runs.

Moving them to `classifying` is also what makes the batch recoverable. A
worker killed mid-batch leaves its rows there, which is precisely what
`rescan_stuck` re-enqueues (as single-email `classify_email` jobs), so no email
is stranded by a crash between the claim and the verdict.

Revision ID: b7d4c1e05f83
Revises: a020a3b90f54
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "b7d4c1e05f83"
down_revision: str | None = "a020a3b90f54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "claim_fetched_email_rows(p_limit int)"
ARG_TYPES = "int"
BODY = """
RETURNS TABLE (id uuid, tenant_id uuid, mailbox_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- tenant_id and mailbox_id come back because every job carries its
    -- tenant; the job re-reads the row under its own tenant policy and reads
    -- nothing if the pair does not match.
    --
    -- SKIP LOCKED so two supervisors — during a deploy, say — split the work
    -- instead of one blocking on the other's claim. ORDER BY updated_at is
    -- oldest-first: a backlog drains in the order it arrived rather than
    -- starving whatever was unlucky enough to be at the back of it.
    UPDATE email_messages e
    SET processing_status = 'classifying'
    WHERE e.id IN (
        SELECT c.id FROM email_messages c
        WHERE c.processing_status = 'fetched'
        ORDER BY c.updated_at
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING e.id, e.tenant_id, e.mailbox_id
$$
"""


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {BODY}")
    # Postgres grants EXECUTE to PUBLIC by default, which would hand an
    # RLS-bypassing function to every role that can connect.
    op.execute(f"REVOKE ALL ON FUNCTION claim_fetched_email_rows({ARG_TYPES}) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' claim_fetched_email_rows({ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS claim_fetched_email_rows({ARG_TYPES})")
