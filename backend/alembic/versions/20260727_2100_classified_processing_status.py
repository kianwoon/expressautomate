"""classified processing status

`processing_status` gained `classified` — the gate has answered, extraction has
not started. Previously that row sat at `classifying`, indistinguishable from a
gate call still in flight, and `rescan_stuck` resolved the ambiguity by
re-enqueueing `classify_email`: a model call for a verdict already stored, every
RESCAN_WORKING_MINUTES, plus a duplicate extraction alongside it.

Three things have to learn the new name here, or a row that reaches it stops
being recoverable at all:

- `stalled_email_rows`, which enumerates the non-terminal statuses. A
  `classified` row missing from it would never be swept, so an extraction that
  never ran would leave the email parked forever with nothing looking for it.
- the partial index the sweep reads through, whose predicate has to cover every
  status that query can ask for or the scan silently stops using it.
- the rows already stuck in production, which are at `classifying` with a
  verdict recorded and would keep being re-billed until they moved.

Revision ID: e3a7c60fd914
Revises: b7d4c1e05f83
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op

from app.core.config import settings

revision: str = "e3a7c60fd914"
down_revision: str | None = "b7d4c1e05f83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIGNATURE = "stalled_email_rows(p_pending_minutes int, p_working_minutes int)"
ARG_TYPES = "int, int"

INDEX = "ix_email_messages_stalled"
_WORKING = "'fetched', 'classifying', 'classified', 'extracting'"
_OLD_WORKING = "'fetched', 'classifying', 'extracting'"


def _body(working: str) -> str:
    return f"""
RETURNS TABLE (id uuid, tenant_id uuid, mailbox_id uuid,
               processing_status text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    -- tenant_id and mailbox_id are returned because every job carries its
    -- tenant. Without them the sweep would enqueue jobs that raise TypeError
    -- inside the worker, and the recovery net would be doing nothing at all
    -- while appearing to run.
    --
    -- Two grace periods: a queue hop should be quick, but a fetch or an
    -- extraction legitimately takes longer, so they are not swept on the same
    -- clock. Terminal statuses are absent on purpose. `classified` is not
    -- terminal — the verdict is stored but the opportunity is not, and the
    -- caller decides which job resumes each status.
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
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {_body(_WORKING)}")
    # Postgres grants EXECUTE to PUBLIC by default, which would hand an
    # RLS-bypassing function to every role that can connect. CREATE OR REPLACE
    # keeps the existing grants, but re-stating them costs nothing and makes
    # this migration correct if it ever runs against a fresh function.
    op.execute(f"REVOKE ALL ON FUNCTION stalled_email_rows({ARG_TYPES}) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION"
        f' stalled_email_rows({ARG_TYPES}) TO "{settings.DATABASE_APP_ROLE}"'
    )
    _swap_index(_WORKING)
    # The rows the bug is currently burning money on. A verdict is already
    # recorded for each, so `classifying` is a lie about them and the sweep
    # acts on that lie by paying the gate again.
    op.execute(
        "UPDATE email_messages SET processing_status = 'classified'"
        " WHERE processing_status = 'classifying'"
        "   AND classification_status <> 'unknown'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE email_messages SET processing_status = 'classifying'"
        " WHERE processing_status = 'classified'"
    )
    _swap_index(_OLD_WORKING)
    op.execute(f"CREATE OR REPLACE FUNCTION {SIGNATURE} {_body(_OLD_WORKING)}")
