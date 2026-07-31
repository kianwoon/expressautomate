"""Transitional tenant-wide shares, so ownership does not land as a blackout.

`c1a0d5e7b201` backfills `candidates.owner_id` from `created_by`, and the
visibility predicate then makes every candidate private to its owner. In a live
agency that would, at the instant of deploy, remove from every recruiter's view
every candidate they did not personally create. Nothing is destroyed — claim
and share recover it — but an unannounced blackout is not a change a revert can
undo, because people have already seen it.

So every candidate that exists at deploy time gets one `scope='tenant'` share.
The database stays exactly as visible as it is today; the agency is told; and
the transitional rows are removed later, at which point the feature's real
privacy takes effect.

**The discriminator: `shared_by_user_id IS NULL`.** These rows record no
author, because nobody performed this share — a real user id would be a lie
about who decided it. Every deliberate broadcast, by contrast, is made by
someone and carries their id. So `scope='tenant' AND shared_by_user_id IS NULL`
selects exactly the rows this migration created, and `downgrade()` deletes only
those.

**The residual, stated plainly:** the sharer FK is `SET NULL`, so a genuine
broadcast whose author is later deleted becomes indistinguishable from a
transitional row and would also be deleted. This is accepted rather than
solved. The alternatives are worse: a marker column would add permanent schema
for a temporary state, and a `note` sentinel would be user-visible text
pretending to be metadata. The blast radius is small and self-correcting — the
row is a tenant-wide *read* grant, deleting it removes no data, and any
recruiter can re-broadcast. The window is also narrow: the removal below is
meant to run within weeks of deploy, and it only bites for a broadcast made
after deploy whose author left before removal.

**Removal path.** A one-line SQL statement, run by an operator, rather than a
second migration — deliberately. The exit is a business decision timed by when
the agency has been told, not by a deploy; a migration would fire it on the
next unrelated release, which is precisely the unannounced change this whole
migration exists to prevent. It is the same statement as `downgrade()`:

    DELETE FROM candidate_shares WHERE scope = 'tenant' AND shared_by_user_id IS NULL;

Run it as the migration/superuser role — RLS is FORCEd on this table, so the
app role would silently delete only the current tenant's rows.

Revision ID: c1a0d5e7b206
Revises: c1a0d5e7b205
Create Date: 2026-07-31 11:00:00+00:00

allow-hardcode: SQL statements, not configuration.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1a0d5e7b206"
down_revision: str | None = "c1a0d5e7b205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Every candidate, whatever its `record_status` — the point is that nothing a
# recruiter can see today disappears, and the visibility predicate, not this
# migration, is what decides which statuses are shown.
#
# ON CONFLICT infers the partial index `uq_candidate_shares_per_tenant` by its
# columns plus its WHERE clause. A partial unique index cannot be named as a
# constraint here, because it is not one.
TRANSITIONAL_SHARE_SQL = """
INSERT INTO candidate_shares (
    id, tenant_id, candidate_id, scope,
    shared_with_user_id, shared_by_user_id, note,
    created_at, updated_at
)
SELECT
    gen_random_uuid(), c.tenant_id, c.id, 'tenant',
    NULL, NULL, NULL,
    now(), now()
FROM candidates AS c
ON CONFLICT (tenant_id, candidate_id) WHERE scope = 'tenant' DO NOTHING
"""

# The discriminator. `shared_by_user_id IS NULL` is what makes these rows
# distinguishable from a broadcast a recruiter made deliberately: nobody
# performed the transitional share, and every real one has an author.
TRANSITIONAL_SHARE_REMOVAL_SQL = """
DELETE FROM candidate_shares
WHERE scope = 'tenant' AND shared_by_user_id IS NULL
"""


def upgrade() -> None:
    op.execute(TRANSITIONAL_SHARE_SQL)


def downgrade() -> None:
    op.execute(TRANSITIONAL_SHARE_REMOVAL_SQL)
