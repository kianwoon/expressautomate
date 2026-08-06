"""CV ingest states and provenance, and the sweep that finds a stranded ingest

A CV uploaded with no candidate named goes through its own front half before
the existing `parse_candidate_cv` reads it: an identity extraction decides who
it belongs to, a match resolves that person to an existing or new candidate,
and only then does the roles/skills parse run. Two new states describe that
front half, and a third describes the one terminal outcome it can reach that
`parse_candidate_cv` cannot.

`ingest_pending` is where the new `POST /candidates/documents` route parks the
bytes — the document is stored, a placeholder candidate holds the foreign key,
and nothing has been read yet. `ingesting` is the job mid-flight. Both resume at
the new `ingest_candidate_cv` job rather than at `parse_candidate_cv`, so the
two jobs are kept apart at the sweep as well as at the enqueue.

`needs_review` is terminal. It is the state for a CV whose identity resolved to
a colleague's candidate the uploader cannot see, or to two different people — a
person must look at it, so the roles/skills parse is deliberately not run and
nothing the document said gets attached to the wrong record.

`origin` records how a document entered the platform. Every existing row is an
upload through the per-candidate drop-zone, so the server default makes the
backfill correct with no data move. Cloud sources share this column when they
land.

`stalled_candidate_documents` is widened so a stranded ingest is recovered by
the right job. A `pending`/`parsing` row resumes at `parse_candidate_cv` exactly
as before; an `ingest_pending`/`ingesting` row resumes at `ingest_candidate_cv`.
The resolver now returns `origin` alongside the columns it already returned, so
`rescan_stuck` can route on it without a second read.

Revision ID: 4989e08d79a4
Revises: c1a0d5e7b218
Create Date: 2026-08-07 10:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "4989e08d79a4"
down_revision: str | None = "c1a0d5e7b218"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = (
    "'ingest_pending','ingesting','pending','parsing',"
    "'parsed','empty','unreadable','failed','needs_review'"
)

_RESOLVER = "stalled_candidate_documents(p_pending_minutes int, p_working_minutes int)"
_RESOLVER_ARGS = "int, int"
_RESOLVER_BODY = """
    RETURNS TABLE (id uuid, tenant_id uuid, candidate_id uuid, parse_state text, origin text)
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
        SELECT d.id, d.tenant_id, d.candidate_id, d.parse_state, d.origin
        FROM candidate_documents d
        WHERE (d.parse_state IN ('ingest_pending','pending')
               AND d.updated_at < now() - make_interval(mins => p_pending_minutes))
           OR (d.parse_state IN ('ingesting','parsing')
               AND d.updated_at < now() - make_interval(mins => p_working_minutes))
    $$"""


def upgrade() -> None:
    op.drop_constraint(
        "ck_candidate_documents_parse_state", "candidate_documents", type_="check"
    )
    op.create_check_constraint(
        "ck_candidate_documents_parse_state",
        "candidate_documents",
        f"parse_state IN ({_STATES})",
    )

    op.add_column(
        "candidate_documents",
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="upload"),
    )

    name = _RESOLVER.split("(", 1)[0]
    # Widen the return signature to carry `origin`, so the sweep can route an
    # `ingest_pending` row to `ingest_candidate_cv` and a `pending` row to
    # `parse_candidate_cv` without a second read. `CREATE OR REPLACE` needs the
    # full body; the old body is dropped by the replacement.
    op.execute(f"DROP FUNCTION IF EXISTS {name}({_RESOLVER_ARGS})")
    op.execute(f"CREATE FUNCTION {_RESOLVER} {_RESOLVER_BODY}")
    op.execute(f"REVOKE ALL ON FUNCTION {name}({_RESOLVER_ARGS}) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {name}({_RESOLVER_ARGS}) "
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )


def downgrade() -> None:
    # Rows in the new states have no home in the old vocabulary. `ingest_*`
    # collapses to `pending` (the front half resumes at the new job, which an
    # older revision has no concept of, so `pending` is the closest honest
    # state); `needs_review` collapses to `failed` (both are terminal and human).
    op.execute(
        "UPDATE candidate_documents SET parse_state = 'pending' "
        "WHERE parse_state IN ('ingest_pending','ingesting')"
    )
    op.execute(
        "UPDATE candidate_documents SET parse_state = 'failed' "
        "WHERE parse_state = 'needs_review'"
    )

    op.drop_column("candidate_documents", "origin")

    op.drop_constraint(
        "ck_candidate_documents_parse_state", "candidate_documents", type_="check"
    )
    op.create_check_constraint(
        "ck_candidate_documents_parse_state",
        "candidate_documents",
        "parse_state IN ('pending','parsing','parsed','empty','unreadable','failed')",
    )
