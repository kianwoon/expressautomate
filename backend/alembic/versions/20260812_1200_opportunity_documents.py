"""opportunity documents: job-description files attached to a job order

Revision ID: c1v2k0000001
Revises: c1v2j0000001
Create Date: 2026-08-12 12:00:00+00:00

One row per uploaded PDF/DOC that the New job order dialog attaches to a
vacancy. The row exists before the opportunity does — the create-dialog flow is
upload, extract, review, save, link — so `opportunity_id` is nullable and is
written by `create_opportunity` when the form carries a `document_id`. Deleting
an opportunity cascades to its documents (composite FK, CASCADE).

Created from the start with RLS, the same way `candidate_job_shortlists` was:
`verify_rls_enforced()` refuses to boot if a `tenant_id` table has no policy,
so this table must carry a `tenant_isolation` policy or the deployment will
not start. FORCE, not merely ENABLE — see `20260727_1900_extraction_tables.py`.

`stalled_opportunity_documents` is the documents-of-job-orders half of
`rescan_stuck`. It is a SECURITY DEFINER resolver for the same reason
`stalled_candidate_documents` is: the sweep runs across every tenant at once
with no `app.tenant_id` set, and `opportunity_documents` carries FORCE ROW
LEVEL SECURITY, so a plain SELECT there matches zero rows silently rather than
erroring.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import settings

revision: str = "c1v2k0000001"
down_revision: str | None = "c1v2j0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED: list[tuple[str, str]] = [("opportunity_documents", "tenant_id")]
SETTING = "app.tenant_id"

_RESOLVER = "stalled_opportunity_documents(p_pending_minutes int, p_working_minutes int)"
_RESOLVER_ARGS = "int, int"
_RESOLVER_BODY = """
    RETURNS TABLE (id uuid, tenant_id uuid, extract_state text)
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
        -- tenant_id travels with the row because the extraction job is
        -- enqueued with it: enqueueing the document id alone would raise
        -- TypeError inside the worker, on the far side of the queue.
        --
        -- Two grace periods, matching `stalled_candidate_documents`: a queue
        -- hop should be quick, but an extraction legitimately spends a model
        -- call. The terminal states are absent on purpose — `extracted`,
        -- `unreadable` and `failed` are answers, not interruptions.
        SELECT d.id, d.tenant_id, d.extract_state
        FROM opportunity_documents d
        WHERE (d.extract_state = 'pending'
               AND d.updated_at < now() - make_interval(mins => p_pending_minutes))
           OR (d.extract_state = 'extracting'
               AND d.updated_at < now() - make_interval(mins => p_working_minutes))
    $$
"""


def upgrade() -> None:
    op.create_table(
        "opportunity_documents",
        sa.Column("opportunity_id", sa.UUID(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("extract_state", sa.String(length=16), nullable=False),
        sa.Column("extract_error", sa.Text(), nullable=True),
        # The extracted values handed to the form for review, in the form's
        # own vocabulary. Null until the worker maps the extraction.
        sa.Column("prefill", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("uploaded_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "extract_state IN ('pending','extracting','extracted','unreadable','failed')",
            name="ck_opportunity_documents_extract_state",
        ),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        # Composite FK to `opportunities(tenant_id, id)`, possible because
        # `opportunities` carries `uq_opportunities_tenant_id_id`. Nullable
        # because the file is stored before the vacancy exists; the link is
        # written by `create_opportunity`. CASCADE so deleting a job order
        # removes its attached files — the bytes are also deleted by the API's
        # delete route, which runs first.
        sa.ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.id"],
            ondelete="CASCADE",
            name="fk_opportunity_documents_opportunity_same_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_opportunity_documents_tenant_id"),
        "opportunity_documents",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_opportunity_documents_opportunity_id"),
        "opportunity_documents",
        ["opportunity_id"],
        unique=False,
    )

    _enforce_rls()
    _touch_updated_at()
    _resolver()


def downgrade() -> None:
    _drop_resolver()
    op.drop_index(
        op.f("ix_opportunity_documents_opportunity_id"),
        table_name="opportunity_documents",
    )
    op.drop_index(
        op.f("ix_opportunity_documents_tenant_id"),
        table_name="opportunity_documents",
    )
    op.drop_table("opportunity_documents")


def _touch_updated_at() -> None:
    """Bind the shared trigger so `updated_at` means what every table's does."""
    op.execute(
        "DROP TRIGGER IF EXISTS opportunity_documents_touch_updated_at "
        "ON opportunity_documents"
    )
    op.execute(
        """
        CREATE TRIGGER opportunity_documents_touch_updated_at
        BEFORE UPDATE ON opportunity_documents
        FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
        """
    )


def _enforce_rls() -> None:
    """FORCE, not merely ENABLE — see `20260727_1900_extraction_tables.py`."""
    for table, column in PROTECTED:
        predicate = f"{column} = nullif(current_setting('{SETTING}', true), '')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )


def _resolver() -> None:
    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"CREATE OR REPLACE FUNCTION {_RESOLVER} {_RESOLVER_BODY}")
    # Postgres grants EXECUTE to PUBLIC by default, which would hand an
    # RLS-bypassing function to every role that can connect.
    op.execute(f"REVOKE ALL ON FUNCTION {name}({_RESOLVER_ARGS}) FROM PUBLIC")
    op.execute(
        f'GRANT EXECUTE ON FUNCTION {name}({_RESOLVER_ARGS}) '
        f'TO "{settings.DATABASE_APP_ROLE}"'
    )


def _drop_resolver() -> None:
    name = _RESOLVER.split("(", 1)[0]
    op.execute(f"DROP FUNCTION IF EXISTS {name}({_RESOLVER_ARGS})")
