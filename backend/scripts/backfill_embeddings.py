"""Backfill candidate embeddings for one tenant.

Embeddings are computed by the `compute_candidate_embedding` worker whenever a
CV is parsed, but candidates that pre-date the feature have parsed CVs and no
vector. This script enqueues the worker for every such candidate in a tenant,
so a deployment that turns the feature on retroactively covers its existing
roster rather than only candidates added from that day forward.

It enqueues rather than embeds inline for two reasons: the worker is the one
path that owns the provider call and its retry/idempotency shape, and a roster
of thousands of CVs embedded serially in one script would take an hour where
the queue spreads the work across worker slots. The script's job is to find
the candidates; the worker's job is to embed them.

Safety mirrors `seed_candidates.py`: this enqueues real work against a real
provider, so it defaults to `--dry-run` (lists what it would enqueue) and
refuses to write without `--write`.

    uv run python scripts/backfill_embeddings.py --tenant-id <uuid> --dry-run
    uv run python scripts/backfill_embeddings.py --tenant-id <uuid> --write
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.rls import tenant_session  # noqa: E402
from app.models.candidate import Candidate, CandidateDocument, CandidateEmbedding  # noqa: E402
from app.workers.embedding_jobs import JOB_COMPUTE_EMBEDDING  # noqa: E402
from app.workers.queue import enqueue  # noqa: E402


async def candidates_needing_embeddings(tenant: uuid.UUID) -> list[uuid.UUID]:
    """Candidates with a parsed CV but no vector for the configured model.

    A candidate may have a vector under a previous model and still appear
    here: the unique key is `(tenant, candidate, model)`, so a model change is
    a backfill, and the old row stays until a human retires it. The check is
    against the *configured* model, because that is what the worker writes.
    """
    async with tenant_session(tenant) as session:
        # Candidates that have at least one parsed document with text.
        doc_subq = (
            select(CandidateDocument.candidate_id)
            .where(
                CandidateDocument.tenant_id == tenant,
                CandidateDocument.parse_state == CandidateDocument.PARSED,
                CandidateDocument.text_key.is_not(None),
            )
            .distinct()
            .scalar_subquery()
        )
        # Candidates already embedded under the configured model.
        embedded_subq = (
            select(CandidateEmbedding.candidate_id)
            .where(
                CandidateEmbedding.tenant_id == tenant,
                CandidateEmbedding.model == settings.EMBEDDING_MODEL,
            )
            .scalar_subquery()
        )
        rows = await session.execute(
            select(Candidate.id)
            .where(
                Candidate.tenant_id == tenant,
                Candidate.record_status == Candidate.ACTIVE,
                Candidate.id.in_(doc_subq),
                ~Candidate.id.in_(embedded_subq),
            )
            .order_by(Candidate.id)
        )
        return [row[0] for row in rows]


async def run(tenant_id: str, *, write: bool) -> int:
    tenant = uuid.UUID(tenant_id)
    candidate_ids = await candidates_needing_embeddings(tenant)
    print(f"Tenant {tenant_id}: {len(candidate_ids)} candidate(s) to embed.")
    if not write:
        if candidate_ids:
            print("Dry run — re-run with --write to enqueue embeddings.")
        return 0

    if not settings.embedding_configured():
        print("EMBEDDING_API_KEY is not set; nothing to enqueue.")
        return 1

    enqueued = 0
    for candidate_id in candidate_ids:
        accepted = await enqueue(
            JOB_COMPUTE_EMBEDDING,
            tenant_id=str(tenant),
            candidate_id=str(candidate_id),
        )
        if accepted:
            enqueued += 1
        else:
            print(f"  skipped (deduplicated or queue error): {candidate_id}")
    print(f"Enqueued {enqueued} of {len(candidate_ids)} embedding job(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, help="Tenant UUID")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually enqueue jobs. Without this, lists what would be enqueued.",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.tenant_id, write=args.write))


if __name__ == "__main__":
    sys.exit(main())
