"""what a CV parse produced, and the sweep that finds a stranded one

Three things, all of them consequences of a document now actually being
parsed rather than merely stored.

`empty` joins the parse states. A CV whose text was read but which yielded no
role and no skill is not a success: it is usually a covering letter, a scanned
page with no text layer behind it, or a prompt regression. Recorded as
`parsed` it would be indistinguishable from a CV that produced a full career,
and the only visible difference would be a candidate panel that stayed blank.

`dropped_count` and `dropped_reason` record what the extractor discarded.
`extract_cv` drops any role or skill whose quotation is not on the page — the
right call, and a silent one: a recruiter whose five-job CV yields three roles
otherwise has no way to learn that the other two were dropped rather than
never seen.

`stalled_candidate_documents` is the documents half of `rescan_stuck`. It is a
SECURITY DEFINER resolver for the same reason `stalled_email_rows` is: the
sweep runs across every tenant at once with no `app.tenant_id` set, and
`candidate_documents` carries FORCE ROW LEVEL SECURITY, so a plain SELECT
there matches zero rows silently rather than erroring.

Revision ID: e2f7b8c15a44
Revises: d1e6a7b04f33
Create Date: 2026-07-29 12:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "e2f7b8c15a44"
down_revision: str | None = "d1e6a7b04f33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES_WITH_EMPTY = "'pending','parsing','parsed','empty','unreadable','failed'"
_STATES_WITHOUT_EMPTY = "'pending','parsing','parsed','unreadable','failed'"

_RESOLVER = "stalled_candidate_documents(p_pending_minutes int, p_working_minutes int)"
_RESOLVER_ARGS = "int, int"
_RESOLVER_BODY = """
    RETURNS TABLE (id uuid, tenant_id uuid, candidate_id uuid, parse_state text)
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
        -- tenant_id and candidate_id travel with the row because
        -- `parse_candidate_cv` takes all three: enqueueing the document id
        -- alone would raise TypeError inside the worker, on the far side of
        -- the queue, and the recovery net would appear to run while doing
        -- nothing.
        --
        -- Two grace periods, matching `stalled_email_rows`: a queue hop
        -- should be quick, but a parse legitimately spends a model call.
        -- The terminal states are absent on purpose — `parsed`, `empty`,
        -- `unreadable` and `failed` are answers, not interruptions.
        SELECT d.id, d.tenant_id, d.candidate_id, d.parse_state
        FROM candidate_documents d
        WHERE (d.parse_state = 'pending'
               AND d.updated_at < now() - make_interval(mins => p_pending_minutes))
           OR (d.parse_state = 'parsing'
               AND d.updated_at < now() - make_interval(mins => p_working_minutes))
    $$
"""


def upgrade() -> None:
    op.drop_constraint(
        "ck_candidate_documents_parse_state", "candidate_documents", type_="check"
    )
    op.create_check_constraint(
        "ck_candidate_documents_parse_state",
        "candidate_documents",
        f"parse_state IN ({_STATES_WITH_EMPTY})",
    )

    op.add_column(
        "candidate_documents",
        sa.Column("dropped_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("candidate_documents", sa.Column("dropped_reason", sa.Text()))

    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"CREATE OR REPLACE FUNCTION {_RESOLVER} {_RESOLVER_BODY}")
    # Postgres grants EXECUTE to PUBLIC by default, which would hand an
    # RLS-bypassing function to every role that can connect.
    op.execute(f"REVOKE ALL ON FUNCTION {name}({_RESOLVER_ARGS}) FROM PUBLIC")
    op.execute(
        f'GRANT EXECUTE ON FUNCTION {name}({_RESOLVER_ARGS}) '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"DROP FUNCTION IF EXISTS {name}({_RESOLVER_ARGS})")

    op.drop_column("candidate_documents", "dropped_reason")
    op.drop_column("candidate_documents", "dropped_count")

    # Rows already in `empty` would violate the narrower constraint, so they
    # are folded back into `parsed` — which is exactly the state this
    # revision existed to tell apart, and the honest cost of going back.
    op.execute("UPDATE candidate_documents SET parse_state = 'parsed' WHERE parse_state = 'empty'")
    op.drop_constraint(
        "ck_candidate_documents_parse_state", "candidate_documents", type_="check"
    )
    op.create_check_constraint(
        "ck_candidate_documents_parse_state",
        "candidate_documents",
        f"parse_state IN ({_STATES_WITHOUT_EMPTY})",
    )
