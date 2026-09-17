"""candidate_identity_resolutions: the Identity Resolver's cache and evidence

Revision ID: c1v2u0000001
Revises: c1v2t0000001
Create Date: 2026-09-03 10:00:00+00:00

Spec: `serper design.md` §24 (the resolved record), §34/§35 (the cache), §47/
§48 (provenance, retention).

One row per Identity Resolver run: the cache entry a repeated click reuses
(§34), and the evidence trail explaining why a candidate was resolved (§47).
`fingerprint_hash` is the §34 cache key — a changed fingerprint (a recruiter
edited the candidate) is a natural cache miss, which is §35's "invalidate
early" rule without a trigger. `status` is a bounded string, not a Postgres
enum, so §59's `stale` state can be added without a type migration.

Created with FORCE RLS like `external_candidate_searches`: `verify_rls_
enforced()` refuses to boot if a `tenant_id` table has no policy. The FK to
`opportunities` is plain (not composite) and CASCADE, because a composite
`SET NULL` would null `tenant_id` — see `ExternalCandidateSearch`.

Source-provenance (CLAUDE.md): `evidence` holds public-web quotes with their
source URLs, retention follows the job order, and raw provider responses are
not stored — only the normalized, attributed evidence (§48).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "c1v2u0000001"
down_revision: str | None = "c1v2t0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROTECTED: list[tuple[str, str]] = [("candidate_identity_resolutions", "tenant_id")]
SETTING = "app.tenant_id"


def upgrade() -> None:
    op.create_table(
        "candidate_identity_resolutions",
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column("candidate_key", sa.Text(), nullable=False),
        sa.Column("fingerprint", JSONB(), nullable=False),
        sa.Column("fingerprint_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("canonical_profile_url", sa.Text(), nullable=True),
        sa.Column("current_company", sa.Text(), nullable=True),
        sa.Column("current_title", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("previous_companies", JSONB(), nullable=True),
        sa.Column("evidence", JSONB(), nullable=True),
        sa.Column("queries_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("freshness_status", sa.String(length=24), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["opportunities.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_candidate_identity_resolutions_tenant_id"),
        "candidate_identity_resolutions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_candidate_identity_resolutions_opportunity_id"),
        "candidate_identity_resolutions",
        ["opportunity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_candidate_identity_resolutions_candidate_key"),
        "candidate_identity_resolutions",
        ["candidate_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_candidate_identity_resolutions_fingerprint_hash"),
        "candidate_identity_resolutions",
        ["fingerprint_hash"],
        unique=False,
    )

    _enforce_rls()
    _touch_updated_at()


def downgrade() -> None:
    for index in (
        "fingerprint_hash",
        "candidate_key",
        "opportunity_id",
        "tenant_id",
    ):
        op.drop_index(
            op.f(f"ix_candidate_identity_resolutions_{index}"),
            table_name="candidate_identity_resolutions",
        )
    op.drop_table("candidate_identity_resolutions")


def _touch_updated_at() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS candidate_identity_resolutions_touch_updated_at "
        "ON candidate_identity_resolutions"
    )
    op.execute(
        """
        CREATE TRIGGER candidate_identity_resolutions_touch_updated_at
        BEFORE UPDATE ON candidate_identity_resolutions
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
