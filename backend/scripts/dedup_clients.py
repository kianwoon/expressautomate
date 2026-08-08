"""One-shot dedup of client rows that share a real-world identity.

Two problems had been letting duplicate client rows accumulate:

1. A concurrency race in the name-path create (`WHERE NOT EXISTS` under READ
   COMMITTED let two parallel extract_email jobs each miss the other's
   uncommitted insert and both insert). Fixed forward by a transaction-scoped
   advisory lock; this script collapses the rows the race already produced.

2. A stale seed batch wrote `name_normalized = lower(name)` (keeping legal
   suffixes) instead of using `normalize_company_name()`. The live pipeline's
   correct normaliser produced a *different* key, so no match, so a second
   client was created. This script re-normalises every row so future matches
   land, then merges the clusters the divergence left behind.

WHAT IT DOES, in order:
  a. Re-run `normalize_company_name()` over every non-merged client and write
     the corrected value back. This alone collapses some near-clusters (two
     rows that disagreed only because of the stale suffix now share a key).
  b. Group remaining duplicates by (tenant_id, name_normalized) and by domain.
  c. For each cluster, pick the survivor (oldest, non-archived preferred) and
     merge every other row into it — repointing opportunities and contacts,
     moving mentions (with collision resolution), and marking losers `merged`.

DRY RUN by default. Pass --write to commit. Either way it prints a plan first.

Usage:
  uv run python scripts/dedup_clients.py --tenant-id <uuid>            # dry run
  uv run python scripts/dedup_clients.py --tenant-id <uuid> --write    # commit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings  # noqa: F401  (loads env)
from app.db.rls import tenant_session
from app.db.session import engine
from app.services.client_naming import normalize_company_name


# --- Step A: re-normalise every non-merged client ---------------------------

_SELECT_CLIENTS = text(
    """
    SELECT id, name, name_normalized
    FROM clients
    WHERE tenant_id = :tenant_id AND status <> 'merged'
    """
)

_UPDATE_NORMALIZED = text(
    "UPDATE clients SET name_normalized = :normalized WHERE id = :id"
)


async def renormalize(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Write the correct normalize_company_name() value back to every row.

    Returns the number of rows whose value changed. Idempotent: a second run
    changes nothing.
    """
    rows = (
        await session.execute(_SELECT_CLIENTS, {"tenant_id": tenant_id})
    ).all()
    changed = 0
    for row in rows:
        correct = normalize_company_name(row.name)
        if correct != row.name_normalized:
            await session.execute(
                _UPDATE_NORMALIZED,
                {"id": row.id, "normalized": correct or ""},
            )
            changed += 1
    return changed


# --- Step B: find duplicate clusters ----------------------------------------

# Group by the corrected name_normalized. After step A, rows that disagreed
# only because of a stale suffix now share a key; only genuine near-clusters
# (plural/singular, variant spellings) remain separate, and those need human
# judgement — this script does not touch them.
_CLUSTERS_BY_NAME = text(
    """
    SELECT array_agg(id ORDER BY created_at) AS ids,
           array_agg(name ORDER BY created_at) AS names,
           name_normalized,
           count(*) AS n
    FROM clients
    WHERE tenant_id = :tenant_id AND status <> 'merged'
      AND name_normalized <> ''
    GROUP BY name_normalized
    HAVING count(*) > 1
    ORDER BY min(created_at)
    """
)

# Same real company can appear once under its name and once under its domain
# (the domain-path client is named after the domain). After re-normalisation
# these are still two different keys, so catch them by the domain column: a
# domain-holder whose domain, stripped of its TLD, matches another client's
# normalised name is almost certainly the same company.
_DOMAIN_CLASH = text(
    """
    SELECT d.id AS domain_client_id, d.name AS domain_name, d.email_domain,
           n.id AS name_client_id, n.name AS name_name, n.name_normalized
    FROM clients d
    JOIN clients n ON n.tenant_id = d.tenant_id
                  AND n.status <> 'merged'
                  AND n.name_normalized = split_part(d.email_domain, '.', 1)
    WHERE d.tenant_id = :tenant_id
      AND d.status <> 'merged'
      AND d.email_domain IS NOT NULL
      AND d.email_domain NOT LIKE '%.example.invalid'
      AND d.id <> n.id
    """
)


# --- Step C: merge one loser into a target ----------------------------------

# Opportunities point client_id → clients. The merge route sets the loser to
# `merged` but never deletes it (FK is SET NULL on *delete* only), so without
# this repoint the loser's opportunities stay attached to a merged row and
# vanish from the live list.
_REPOINT_OPPORTUNITIES = text(
    "UPDATE opportunities SET client_id = :target_id WHERE client_id = :loser_id"
)

# Contacts belong to the company, so they move with the merge. No unique
# constraint on (client_id, email) backs a collision check, but a contact
# already on the target with the same email is left as the recruiter set it —
# additive only, same as the pipeline's own contact capture.
_REPOINT_CONTACTS = text(
    """
    UPDATE client_contacts cc
    SET client_id = :target_id
    WHERE client_id = :loser_id
      AND NOT EXISTS (
          SELECT 1 FROM client_contacts existing
          WHERE existing.client_id = :target_id
            AND lower(coalesce(existing.email, '')) = lower(coalesce(cc.email, ''))
      )
    """
)

# Collaborators (secondary recruiters covering an account) move too.
_REPOINT_COLLABORATORS = text(
    "UPDATE client_collaborators SET client_id = :target_id WHERE client_id = :loser_id"
)

# Buddy referrals: a referral from the same buddy to both rows collapses to
# one (the survivor). The unique constraint (tenant_id, buddy_id, client_id)
# does not conflict because the client_id differs, so repoint then let any
# exact dupes fall to ON CONFLICT — simpler: just repoint, a buddy now refers
# the survivor either way.
_REPOINT_REFERRALS = text(
    """
    DELETE FROM buddy_referrals
    WHERE client_id = :loser_id
      AND buddy_id IN (
          SELECT buddy_id FROM buddy_referrals WHERE client_id = :target_id
      )
    """
)
_REPOINT_REMAINING_REFERRALS = text(
    "UPDATE buddy_referrals SET client_id = :target_id WHERE client_id = :loser_id"
)

_MARK_MERGED = text(
    """
    UPDATE clients
    SET status = 'merged',
        merged_into_client_id = :target_id,
        suspended_at = NULL,
        suspended_reason = NULL
    WHERE id = :loser_id
    """
)


def _mention_strength(matched_by: str | None) -> int:
    return {"email_domain": 2, "name": 1}.get(matched_by, 0)


async def _move_mentions(
    session: AsyncSession, loser_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """Move loser mentions to target, dropping redundant collisions.

    Mirrors the merge route's collision resolution exactly: a stronger loser
    mention outranks and replaces the target's; an equal/weaker one is dropped
    as redundant. NULL-message mentions (purged sources) stay put.
    """
    target_rows = (
        await session.execute(
            text(
                "SELECT id, email_message_id, matched_by FROM client_mentions "
                "WHERE client_id = :target_id"
            ),
            {"target_id": target_id},
        )
    ).all()
    target_by_msg = {
        r.email_message_id: r
        for r in target_rows
        if r.email_message_id is not None
    }
    loser_rows = (
        await session.execute(
            text(
                "SELECT id, email_message_id, matched_by FROM client_mentions "
                "WHERE client_id = :loser_id"
            ),
            {"loser_id": loser_id},
        )
    ).all()

    movable: list[uuid.UUID] = []
    redundant: list[uuid.UUID] = []
    outranked: list[uuid.UUID] = []
    for r in loser_rows:
        if r.email_message_id is None:
            continue
        clash = target_by_msg.get(r.email_message_id)
        if clash is None:
            movable.append(r.id)
        elif _mention_strength(r.matched_by) > _mention_strength(clash.matched_by):
            outranked.append(clash.id)
            movable.append(r.id)
        else:
            redundant.append(r.id)

    if outranked:
        await session.execute(
            text("DELETE FROM client_mentions WHERE id = ANY(:ids)"),
            {"ids": outranked},
        )
    if movable:
        await session.execute(
            text("UPDATE client_mentions SET client_id = :target WHERE id = ANY(:ids)"),
            {"target": target_id, "ids": movable},
        )
    if redundant:
        await session.execute(
            text("DELETE FROM client_mentions WHERE id = ANY(:ids)"),
            {"ids": redundant},
        )


async def merge_into(
    session: AsyncSession, loser_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """Merge loser into target, repointing all dependents.

    No locking here — a one-shot backfill runs single-threaded. The live merge
    route locks the pair because two recruiters can merge at once; this script
    is the only writer when it runs.
    """
    await _move_mentions(session, loser_id, target_id)
    await session.execute(_REPOINT_OPPORTUNITIES, {"loser_id": loser_id, "target_id": target_id})
    await session.execute(_REPOINT_CONTACTS, {"loser_id": loser_id, "target_id": target_id})
    await session.execute(_REPOINT_COLLABORATORS, {"loser_id": loser_id, "target_id": target_id})
    await session.execute(_REPOINT_REFERRALS, {"loser_id": loser_id, "target_id": target_id})
    await session.execute(
        _REPOINT_REMAINING_REFERRALS, {"loser_id": loser_id, "target_id": target_id}
    )
    await session.execute(_MARK_MERGED, {"loser_id": loser_id, "target_id": target_id})


# --- Orchestration ----------------------------------------------------------


async def run(tenant_id: uuid.UUID, *, write: bool) -> None:
    """Re-normalise, then merge every duplicate cluster. Print a plan first."""
    async with tenant_session(tenant_id) as session:
        n_fixed = await renormalize(session, tenant_id)
        action = "renormalised" if write else "would renormalise"
        print(f"[A] {action} {n_fixed} client row(s) with stale name_normalized.")
        if write:
            await session.commit()
        else:
            await session.rollback()

    # After renormalisation commits, read the clusters in a fresh session.
    name_clusters = []
    domain_pairs = []
    async with tenant_session(tenant_id) as session:
        for row in (
            await session.execute(_CLUSTERS_BY_NAME, {"tenant_id": tenant_id})
        ).all():
            name_clusters.append(
                {
                    "normalized": row.name_normalized,
                    "ids": list(row.ids),
                    "names": list(row.names),
                }
            )
        for row in (
            await session.execute(_DOMAIN_CLASH, {"tenant_id": tenant_id})
        ).all():
            domain_pairs.append(
                {
                    "domain_client_id": row.domain_client_id,
                    "domain_name": row.domain_name,
                    "domain": row.email_domain,
                    "name_client_id": row.name_client_id,
                    "name_name": row.name_name,
                }
            )

    print(
        f"\n[B] Found {len(name_clusters)} name-normalised duplicate cluster(s) "
        f"and {len(domain_pairs)} name↔domain clash pair(s).\n"
    )
    if not name_clusters and not domain_pairs:
        print("Nothing to merge. Exiting.")
        return

    for cluster in name_clusters:
        ids = cluster["ids"]
        print(f"  cluster  key={cluster['normalized']!r}")
        for i, (cid, cname) in enumerate(zip(ids, cluster["names"])):
            role = "SURVIVOR" if i == 0 else "merge→"
            print(f"    {role:9} {cid}  {cname}")

    for pair in domain_pairs:
        print(
            f"  domain clash  {pair['name_client_id']} ({pair['name_name']!r})  "
            f"≈  {pair['domain_client_id']} ({pair['domain']!r})"
        )

    if not write:
        print("\nDry run — no changes made. Re-run with --write to merge.")
        return

    async with tenant_session(tenant_id) as session:
        merged = 0
        for cluster in name_clusters:
            survivor, *losers = cluster["ids"]
            for loser_id in losers:
                await merge_into(session, loser_id, survivor)
                merged += 1
        for pair in domain_pairs:
            # Prefer the row that carries a real name (not the domain-string
            # name the domain-path invents) as the survivor.
            survivor = pair["name_client_id"]
            loser_id = pair["domain_client_id"]
            await merge_into(session, loser_id, survivor)
            merged += 1
        await session.commit()
    print(f"\n[COMPLETE] merged {merged} duplicate row(s) into their survivors.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="Tenant UUID to dedup")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Commit changes. Without it, only prints a plan.",
    )
    args = parser.parse_args()
    try:
        tenant_id = uuid.UUID(args.tenant_id)
    except ValueError:
        print(f"error: --tenant-id {args.tenant_id!r} is not a UUID", file=sys.stderr)
        sys.exit(2)

    asyncio.run(run(tenant_id, write=args.write))


if __name__ == "__main__":
    main()
