"""candidate embeddings (pgvector)

Revision ID: c1a0d5e7b212
Revises: c1a0d5e7b211
Create Date: 2026-08-05 20:00:00+00:00

Adds a `candidate_embeddings` table backed by the pgvector extension so the
sourcing scorer can rank candidates by CV-to-job-order semantic similarity.

The table is a derivative artefact, like `candidate_documents.text_key`: the
authoritative text lives in R2, and the vector is a recomputeable function of
it. It is kept in its own table — rather than a column on `candidates` — so
that re-embedding under a different model is an upsert here, not a churn of the
core person record's `updated_at`.

Privacy: the text sent to the embedding provider is CV text that already leaves
the system for LLM explanations (DeepSeek). Embeddings add no new data
boundary; they add a second provider, gated by `EMBEDDING_API_KEY`.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "c1a0d5e7b212"
down_revision: str | None = "c1a0d5e7b211"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SETTING = "app.tenant_id"
PROTECTED = [("candidate_embeddings", "tenant_id")]


def upgrade() -> None:
    # The extension is idempotent and shared across the database; the downgrade
    # does NOT drop it because another table or future feature may depend on it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "candidate_embeddings",
        sa.Column("candidate_id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        # `vector(1536)` matches text-embedding-3-small; the model/dim columns
        # record which embedding produced the row, so a future model swap is a
        # new (candidate, model) row rather than an overwrite.
        sa.Column("embedding", sa.Text(), nullable=False),  # placeholder; see below
        sa.Column("id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", PgUUID(as_uuid=True), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_embeddings_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "model",
            name="uq_candidate_embeddings_once_per_model",
        ),
    )

    # Alembic's autogenerate cannot emit the `vector` type without importing the
    # pgvector SQLAlchemy dialect at generation time. The column above was
    # declared Text solely to keep the table creation dialect-neutral; fix the
    # type in place now. `vector` is an extension type, so plain SQL is the
    # reliable way to set it.
    op.execute("ALTER TABLE candidate_embeddings ALTER COLUMN embedding TYPE vector(1536) USING embedding::vector(1536)")

    op.create_index(
        op.f("ix_candidate_embeddings_tenant_id"),
        "candidate_embeddings",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_candidate_embeddings_candidate_id"),
        "candidate_embeddings",
        ["candidate_id"],
    )
    # HNSW for sub-linear approximate nearest-neighbour search. The dataset is
    # per-tenant-thousands, where exact search would also work, but HNSW is the
    # right default and scales without a migration if a tenant grows.
    # `vector_cosine_ops` because the embedding provider's vectors are used as
    # cosine similarity (L2-normalised at write time, so cosine <=> dot product).
    op.execute(
        "CREATE INDEX ix_candidate_embeddings_hnsw "
        "ON candidate_embeddings USING hnsw (embedding vector_cosine_ops)"
    )

    _enforce_rls()


def _enforce_rls() -> None:
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


def downgrade() -> None:
    op.drop_index(
        op.f("ix_candidate_embeddings_hnsw"),
        table_name="candidate_embeddings",
    )
    op.drop_index(
        op.f("ix_candidate_embeddings_candidate_id"),
        table_name="candidate_embeddings",
    )
    op.drop_index(
        op.f("ix_candidate_embeddings_tenant_id"),
        table_name="candidate_embeddings",
    )
    op.drop_table("candidate_embeddings")
    # Intentionally do NOT drop the `vector` extension: it is shared and may be
    # used by other features. Dropping it here would break any co-tenant table
    # that came to depend on it.
