"""replaying status and claim_replay_email_rows

A deliberate *replay* — re-running extraction on an already-`extracted` email
under a newer prompt version — is the one case the deterministic opportunity id
plus `ON CONFLICT (id) DO NOTHING` makes invisible: the new `extractions` row
and its evidence land, but the improved field values are discarded. That is
correct for a crash-retry (same prompt, same run, nothing changed) and wrong
for a replay (the point of re-running is a better answer).

This migration gives replay a state and a claim:

- `replaying` joins the non-terminal statuses. A worker killed mid-replay leaves
  the email there, and `rescan_stuck` — which now knows the status through
  `stalled_email_rows` and the partial index — re-enqueues `replay_email`, so a
  crash recovers the *replay*, not a plain extraction that would no-op again.
- `claim_replay_email_rows` is the sweep's claim, a SECURITY DEFINER resolver in
  the same pattern as `claim_fetched_email_rows`: the sweep runs with no tenant
  context and `email_messages` carries FORCE ROW LEVEL SECURITY, so a direct
  UPDATE would match nothing at all, silently. The claim moves a bounded set of
  emails whose LATEST extraction used an older prompt than the one now running
  to `replaying` in the same statement that returns them — so two sweeps cannot
  hand the same email to two workers.

Revision ID: c1v2f0000001
Revises: c1v2e0000001
Create Date: 2026-08-11
"""

from collections.abc import Sequence

from alembic import op
from app.core.config import settings

revision: str = "c1v2f0000001"
down_revision: str | None = "c1v2e0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESOLVER_NAME = "stalled_email_rows"
RESOLVER_SIGNATURE = "stalled_email_rows(p_pending_minutes int, p_working_minutes int)"
RESOLVER_ARG_TYPES = "int, int"
CLAIM_NAME = "claim_replay_email_rows"
CLAIM_SIGNATURE = "claim_replay_email_rows(p_limit int, p_prompt_version text)"
CLAIM_ARG_TYPES = "int, text"

# `replaying` joins the working set: it is an interruption, not an outcome, so
# the recovery sweep must see it or a killed replay strands the email forever.
_WORKING = "'fetched', 'classifying', 'classified', 'extracting', 'replaying'"
_OLD_WORKING = "'fetched', 'classifying', 'classified', 'extracting'"

INDEX = "ix_email_messages_stalled"

CLAIM_BODY = """
RETURNS TABLE (id uuid, tenant_id uuid, mailbox_id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- An email is replayable when its most recent extraction ran under a
    -- prompt older than the one the deployment is now running with. The
    -- subquery orders by created_at (tie-broken by id, which is a uuid4 minted
    -- in persist order) so the LATEST extraction is the one compared, and
    -- `IS DISTINCT FROM` treats an email with no extraction row as stale too —
    -- a row that reached a terminal state without one is broken, and replaying
    -- it is the cheapest way to find out.
    --
    -- SKIP LOCKED + ORDER BY updated_at: two supervisors split the work, and a
    -- backlog drains oldest-first. Same idiom as `claim_fetched_email_rows`.
    UPDATE email_messages e
    SET processing_status = 'replaying'
    WHERE e.id IN (
        SELECT c.id FROM email_messages c
        WHERE c.processing_status IN ('extracted', 'no_opportunity')
          AND (SELECT ex.prompt_version FROM extractions ex
               WHERE ex.email_message_id = c.id
               ORDER BY ex.created_at DESC, ex.id DESC
               LIMIT 1) IS DISTINCT FROM p_prompt_version
        ORDER BY c.updated_at
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING e.id, e.tenant_id, e.mailbox_id
$$
"""


def _resolver_body(working: str) -> str:
    return f"""
RETURNS TABLE (id uuid, tenant_id uuid, mailbox_id uuid,
               processing_status text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT e.id, e.tenant_id, e.mailbox_id, e.processing_status
    FROM email_messages e
    WHERE (e.processing_status = 'pending'
           AND e.updated_at < now()
               - make_interval(mins => p_pending_minutes))
       OR (e.processing_status IN ({working})
           AND e.updated_at < now()
               - make_interval(mins => p_working_minutes))
$$
"""


def _swap_index(predicate: str) -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(
        f"CREATE INDEX {INDEX} ON email_messages (processing_status, updated_at)"
        f" WHERE processing_status IN ('pending', {predicate})"
    )


def upgrade() -> None:
    # `replaying` must be visible to the recovery sweep, which reads through
    # `stalled_email_rows` and the partial index it scans.
    op.execute(f"CREATE OR REPLACE FUNCTION {RESOLVER_SIGNATURE} {_resolver_body(_WORKING)}")
    op.execute(f"REVOKE ALL ON FUNCTION {RESOLVER_NAME}({RESOLVER_ARG_TYPES}) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' {RESOLVER_NAME}({RESOLVER_ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )
    _swap_index(_WORKING)

    # The sweep's claim. SECURITY DEFINER for the same reason every resolver is:
    # this process sets no `app.tenant_id` and `email_messages` carries FORCE
    # ROW LEVEL SECURITY, so a direct UPDATE would silently match nothing.
    op.execute(f"CREATE OR REPLACE FUNCTION {CLAIM_SIGNATURE} {CLAIM_BODY}")
    op.execute(f"REVOKE ALL ON FUNCTION {CLAIM_NAME}({CLAIM_ARG_TYPES}) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' {CLAIM_NAME}({CLAIM_ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {CLAIM_SIGNATURE}")
    op.execute(f"CREATE OR REPLACE FUNCTION {RESOLVER_SIGNATURE} {_resolver_body(_OLD_WORKING)}")
    op.execute(f"REVOKE ALL ON FUNCTION {RESOLVER_NAME}({RESOLVER_ARG_TYPES}) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' {RESOLVER_NAME}({RESOLVER_ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )
    _swap_index(_OLD_WORKING)
