"""mom_occupations + job_intelligence.occupation

Revision ID: c1a0d5e7b218
Revises: c1a0d5e7b217
Create Date: 2026-08-06 14:00:00+00:00

Two changes for the salary-benchmark feature:

1. `mom_occupations` — the MOM Resident Occupational Wages (June 2024)
   reference library. Global reference data, the first non-tenant-scoped table
   in the schema. It carries a permissive `USING (true)` RLS policy rather than
   a `tenant_isolation` one, because every agency benchmarks against the same
   Singapore market figures; `verify_rls_enforced()` still demands FORCE ROW
   LEVEL SECURITY, which this migration turns on. Writes happen only from the
   seed script under the admin role (BYPASSRLS), and the application role has
   no DML grant, so a tenant session cannot mutate it.

2. `job_intelligence.occupation` — a nullable JSONB column holding the
   occupation-matching stage's result (matched MOM title, P25/Median/P75,
   confidence, rationale), mirroring the three existing JSONB stage columns.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID


revision: str = "c1a0d5e7b218"
down_revision: str | None = "c1a0d5e7b217"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The extension is shared with candidate_embeddings; idempotent, and the
    # downgrade does not drop it for the same reason.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "mom_occupations",
        sa.Column("id", PgUUID(as_uuid=True), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("gross_p25", sa.Numeric(10, 2), nullable=False),
        sa.Column("gross_p50", sa.Numeric(10, 2), nullable=False),
        sa.Column("gross_p75", sa.Numeric(10, 2), nullable=False),
        sa.Column("basic_p25", sa.Numeric(10, 2), nullable=False),
        sa.Column("basic_p50", sa.Numeric(10, 2), nullable=False),
        sa.Column("basic_p75", sa.Numeric(10, 2), nullable=False),
        # placeholder Text; cast to vector(1536) below — Alembic cannot emit the
        # extension type without importing the pgvector dialect at generate time.
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("year", "title", name="uq_mom_occupations_year_title"),
    )
    op.create_index(
        op.f("ix_mom_occupations_year"),
        "mom_occupations",
        ["year"],
    )
    op.execute(
        "ALTER TABLE mom_occupations ALTER COLUMN embedding "
        "TYPE vector(1536) USING embedding::vector(1536)"
    )
    # HNSW + cosine, matching candidate_embeddings. ~560 rows is tiny enough
    # for exact search, but HNSW is the right default and scales without a
    # migration when future vintages or a richer duties/skills KB arrive.
    op.execute(
        "CREATE INDEX ix_mom_occupations_hnsw "
        "ON mom_occupations USING hnsw (embedding vector_cosine_ops)"
    )

    # RLS: FORCE + a permissive SELECT policy. This is the load-bearing
    # difference from a tenant-scoped table — the boot guard
    # `verify_rls_enforced()` flags any readable table lacking FORCE RLS, so a
    # global reference table must still force RLS on; it then admits every row
    # through the policy. No INSERT/UPDATE/DELETE policy is created: writes are
    # the admin role's job (BYPASSRLS), and the absence of DML policy means a
    # tenant session could not mutate it even if it somehow tried.
    op.execute("ALTER TABLE mom_occupations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mom_occupations FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS mom_occupations_read ON mom_occupations")
    op.execute(
        "CREATE POLICY mom_occupations_read ON mom_occupations "
        "FOR SELECT USING (true)"
    )

    # The occupation stage result on the analysis row.
    op.add_column(
        "job_intelligence",
        sa.Column("occupation", JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_intelligence", "occupation")

    op.execute("DROP POLICY IF EXISTS mom_occupations_read ON mom_occupations")
    op.execute("DROP INDEX IF EXISTS ix_mom_occupations_hnsw")
    op.drop_index(op.f("ix_mom_occupations_year"), table_name="mom_occupations")
    op.drop_table("mom_occupations")
    # Intentionally do NOT drop the `vector` extension: shared with
    # candidate_embeddings and may serve future features.
