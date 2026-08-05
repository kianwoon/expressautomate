"""backfill sex_requirement from detected shorthand codes

Revision ID: c1a0d5e7b214
Revises: c1a0d5e7b213
Create Date: 2026-08-05 22:00:00+00:00

Populates `opportunities.sex_requirement` / `sex_requirement_reason` for
opportunities whose `opportunity_codes` imply a sex the client asked for
(`C/F`/`O/F` → female, `C/M`/`O/M` → male) but were ingested before this was set
at insert time.

A data migration is the only honest way to reach rows that already exist: the
ingestion change only affects new emails, and an agency's existing book — the
job orders a recruiter is looking at right now — would otherwise stay on "None"
until each email is reprocessed, which nobody does by hand.

The sex is derived with the SAME `implied_sex` function the ingestion path uses
(`app.services.sourcing.preference`), so the backfill and new writes can never
disagree about what a set of codes means. A pure function in a data migration is
unusual; it is the right call here because the alternative — re-implementing the
agreement/meaning logic in SQL — would be a second copy that drifts, and a
mismatch between backfilled rows and freshly-ingested ones is exactly the kind
of silent inconsistency an audit trail must not develop.

Only rows with no `sex_requirement` yet are touched — a recruiter who already
set one (with their own reason) is never overwritten. Conflicting codes (both
sexes named) and no-sex codes are skipped, matching `implied_sex`'s `None`.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c1a0d5e7b214"
down_revision: str | None = "c1a0d5e7b213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Imported lazily, inside the migration, so the app package is only needed
    # where the migration actually runs (the deployed environment), not at
    # revision-discovery time.
    from app.services.sourcing.preference import implied_sex

    bind = op.get_bind()
    # Load every opportunity that has no sex requirement yet, with its codes.
    # Reads as the admin role the migration runs under; this is a one-off and
    # touches every tenant's rows, which a tenant-scoped session could not.
    rows = bind.execute(
        sa.text(
            """
            SELECT o.id, o.tenant_id,
                   array_agg(oc.code ORDER BY oc.start_char) AS codes,
                   array_agg(oc.meaning ORDER BY oc.start_char) AS meanings
              FROM opportunities o
              JOIN opportunity_codes oc ON oc.opportunity_id = o.id
             WHERE o.sex_requirement IS NULL
          GROUP BY o.id, o.tenant_id
            """
        )
    ).all()

    updated = 0
    for row in rows:
        # Reconstruct the lightweight objects `implied_sex` reads (it only needs
        # `meaning`). A namespace beats a dataclass here: it is a local
        # adaptation, not a type the rest of the codebase shares.
        codes = [
            type("_C", (), {"meaning": meaning, "code": code, "attribute": None})()
            for code, meaning in zip(row.codes, row.meanings)
        ]
        sex = implied_sex(codes)
        if sex is None:
            continue
        found = ", ".join(sorted(set(row.codes)))
        reason = f"Set from the client's shorthand in the source email: {found}."
        bind.execute(
            sa.text(
                "UPDATE opportunities"
                "   SET sex_requirement = :sex, sex_requirement_reason = :reason"
                " WHERE id = :id AND sex_requirement IS NULL"
            ),
            {"sex": sex, "reason": reason, "id": row.id},
        )
        updated += 1

    op.get_context().impl.output_buffer.write(f"\nBackfilled {updated} opportunity rows.\n")


def downgrade() -> None:
    # No downgrade: clearing a requirement that was derived from real evidence
    # would leave a row that reads as though the client stated no preference,
    # which is the one state this migration exists to correct. A revert that
    # also rolled back the ingestion change would be the honest path, and that
    # is a code change, not a data migration.
    pass
