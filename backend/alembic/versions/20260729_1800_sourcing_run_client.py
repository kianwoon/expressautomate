"""what client a sourcing run excluded against, and why it could not

Three nullable columns on `sourcing_runs`, all of them there so a run stays a
record rather than a claim.

`client_id` is the client the eligibility rule "not already submitted to this
client" was applied against. Until now nothing wrote it down: the worker
inferred the client from `client_mentions` on the job order's source email
every time it ran, and substituted a nil UUID when it could not — which made
the exclusion silently do nothing, with no trace on the row that it had. The
inference now happens once, in the route, and lands here. Persisting it also
outlives the inference: `client_mentions.email_message_id` is `ON DELETE SET
NULL`, so a retention purge eventually breaks the join the worker used and a
re-inference of an old run would answer differently from the run itself.

The FK is composite — `(tenant_id, client_id)` against `clients(tenant_id,
id)` — because a plain FK to `clients.id` would let a run point at another
agency's client. It carries no `ON DELETE` action on purpose: `SET NULL` on a
composite would null `tenant_id` too, which is NOT NULL here (the trap
`CandidateImport.import_id` documents), and `CASCADE` would delete the run,
destroying the record of a shortlist a recruiter may have already sent. A
client is merged or archived rather than deleted, so refusing the delete is
the honest end of that trade.

`client_unresolved_reason` is the other half. When no single client can be
resolved the run goes ahead anyway — refusing would kill the feature for
every job order whose client was never matched — but it must say so, in a
sentence the panel can show, because a shortlist that quietly skipped the
already-submitted check is the re-pitching mistake the rule exists to
prevent.

`failure_reason` is where the route's "saved but not queued" message lands. A
run has no error column and no report file, unlike an import, so without one
an enqueue failure would leave a `failed` row with nothing on it to explain
itself or to tell the recruiter that retrying is worth doing.

No RLS work here: `sourcing_runs` already carries a forced `tenant_isolation`
policy from `c1d4e8f29a3b`, and a policy is per table, not per column.

Revision ID: f4b8c1e7d290
Revises: d2f6a41b8c73
Create Date: 2026-07-29 18:00:00+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PgUUID

revision: str = "f4b8c1e7d290"
down_revision: str | None = "d2f6a41b8c73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sourcing_runs", sa.Column("client_id", PgUUID(as_uuid=True), nullable=True))
    op.add_column("sourcing_runs", sa.Column("client_unresolved_reason", sa.Text(), nullable=True))
    op.add_column("sourcing_runs", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_sourcing_runs_client_same_tenant",
        "sourcing_runs",
        "clients",
        ["tenant_id", "client_id"],
        ["tenant_id", "id"],
    )
    op.create_index("ix_sourcing_runs_client_id", "sourcing_runs", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_sourcing_runs_client_id", table_name="sourcing_runs")
    op.drop_constraint("fk_sourcing_runs_client_same_tenant", "sourcing_runs", type_="foreignkey")
    op.drop_column("sourcing_runs", "failure_reason")
    op.drop_column("sourcing_runs", "client_unresolved_reason")
    op.drop_column("sourcing_runs", "client_id")
