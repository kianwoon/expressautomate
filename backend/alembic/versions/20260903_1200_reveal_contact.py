"""reveal contact: public_emails + contact_enrichment on identity resolutions

Revision ID: c1v2v0000001
Revises: c1v2u0000001
Create Date: 2026-09-03 12:00:00+00:00

Phase 3 first slice (Reveal Contact). Two nullable JSONB columns on the
existing resolution row — no new table, no RLS change (the row is already
tenant-scoped and FORCE-RLS protected):

- `public_emails`: the free, unverified addresses mined from the Serper titles/
  snippets and resolved page URLs the run already fetched. Each item is
  `{email, source_url, verified: false}`; provenance travels with the value
  (CLAUDE.md). No server-side page fetch — this slice never stores a raw page.
- `contact_enrichment`: the vendor answer, written only when a provider is
  configured. NULL means "not yet revealed"; a Noop answer is stored as
  `{status: "no_provider"}`, so "never asked" and "asked, no provider" stay
  distinguishable.

Both default to `[]`/NULL for existing rows: an old resolution simply has no
contacts, which is the truth — it was resolved before this pass existed.

Down-migration drops both columns; the derived data is re-extractable from the
same evidence, so nothing irreplaceable is lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "c1v2v0000001"
down_revision: str | None = "c1v2u0000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candidate_identity_resolutions",
        sa.Column(
            "public_emails",
            JSONB(),
            nullable=True,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "candidate_identity_resolutions",
        sa.Column("contact_enrichment", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidate_identity_resolutions", "contact_enrichment")
    op.drop_column("candidate_identity_resolutions", "public_emails")
