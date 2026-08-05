"""arq jobs that turn candidate CVs into vectors.

Its own module for the same reason `sourcing_jobs.py` and `import_jobs.py` are
separate from `jobs.py`: nothing shares with mail ingestion but the queue, and
a job that talks to an embeddings provider has its own failure modes (a 400 on
a too-long input, a provider outage) that deserve their own retry and logging
shape rather than being threaded through a file already at the repo's ceiling.

**Embeddings are a derivative, not a source of truth.** The candidate row, its
roles and skills, and the parsed CV text in R2 are the records; the vector is
a recomputeable function of all three. So this job is idempotent — the same
candidate embedded twice produces the same row via the
`(tenant_id, candidate_id, model)` unique key — and a failure here never blocks
a CV parse or a sourcing run. The scorer abstains on a candidate with no
embedding, exactly as it abstains on a candidate with no salary.

**Graceful absence is the whole design.** If `EMBEDDING_API_KEY` is unset, the
job returns immediately: no provider call, no row written, no error raised.
A deployment that has not opted into semantic matching runs the six-component
scorer unchanged, and this job is a no-op the queue dispatches and forgets.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import (
    Candidate,
    CandidateDocument,
    CandidateEmbedding,
    CandidateRole,
    CandidateSkill,
)
from app.services.llm.embeddings import EmbeddingsError, embed_texts
from app.services.sourcing.embed import candidate_text_for_embedding, truncate
from app.services.storage.r2 import R2BodyStore

log = get_logger(__name__)

# The job name producers enqueue. Kept as a constant rather than relying on
# the function's `__name__` because the registration in `settings.py` can name
# a function differently from its attribute, and a typo between the enqueue
# string and the registered name fails silently (the job is accepted and never
# runs). One source of truth for the string closes that gap.
JOB_COMPUTE_EMBEDDING = "compute_candidate_embedding"


def body_store():
    """Indirection point, so tests can swap in the in-memory store."""
    return R2BodyStore()


async def compute_candidate_embedding(
    ctx,
    *,
    tenant_id: str,
    candidate_id: str,
) -> None:
    """Embed one candidate's CV and upsert the vector.

    Triggered after a CV parse completes (the text is in R2 at that moment)
    and by the backfill for candidates that pre-date this feature. The job
    carries its tenant like every other job here; RLS decides whether the row
    is visible, and a mismatched `(tenant, candidate)` pair reads nothing and
    writes nothing.

    Returns early — not as an error — when embeddings are not configured or the
    candidate has no text to embed. The former is a deployment that has not
    opted in; the latter is a candidate whose CV is still `pending` or failed
    to parse. Both are facts about the environment, not failures to retry.
    """
    if not settings.embedding_configured():
        # A no-op, not a warning: a deployment without an embeddings key runs
        # this job on every CV parse and would log a line each time otherwise.
        # The scorer's graceful degradation depends on this returning quietly.
        return

    tenant = uuid.UUID(tenant_id)
    candidate_key = uuid.UUID(candidate_id)

    async with tenant_session(tenant) as session:
        candidate = await session.get(Candidate, candidate_key)
        if candidate is None:
            log.info("embedding_skipped_unknown_candidate", candidate_id=candidate_id)
            return

        roles = list(
            (
                await session.execute(
                    select(CandidateRole)
                    .where(
                        CandidateRole.tenant_id == tenant,
                        CandidateRole.candidate_id == candidate_key,
                    )
                    .order_by(CandidateRole.started_on)
                )
            ).scalars()
        )
        skills = list(
            (
                await session.execute(
                    select(CandidateSkill).where(
                        CandidateSkill.tenant_id == tenant,
                        CandidateSkill.candidate_id == candidate_key,
                    )
                )
            ).scalars()
        )

        text = await _candidate_text(session, tenant, candidate_key)
        if not text:
            # No parsed CV and no structured fields: nothing to embed. A
            # candidate created with only a name and email has no job-related
            # signal, and embedding an empty string produces a vector that
            # matches nothing honestly and everything by noise.
            log.info("embedding_skipped_no_text", candidate_id=candidate_id)
            return

        assembled = candidate_text_for_embedding(candidate, roles, skills)
        # The structured fields lead, then the CV prose: a title and skill list
        # are the strongest job-related signals, and the prose supplies the
        # detail they abbreviate. Concatenating rather than embedding
        # separately keeps one vector per candidate, which is what the ANN
        # query assumes.
        full = truncate(
            assembled + "\n" + text if assembled else text,
            settings.EMBEDDING_MAX_CHARS,
        )
        if not full.strip():
            return

        try:
            result = await embed_texts([full])
        except EmbeddingsError:
            # Classed, not retried here: arq's `max_tries` handles transient
            # failures, and a 400 (input too long, encoding issue) will not be
            # fixed by asking again. Logging at warning so a persistent
            # provider problem is visible without failing the CV parse.
            log.warning("embedding_failed", candidate_id=candidate_id, exc_info=True)
            return

        vector = result.vectors[0]
        await session.execute(
            pg_insert(CandidateEmbedding)
            .values(
                tenant_id=tenant,
                candidate_id=candidate_key,
                model=settings.EMBEDDING_MODEL,
                dim=settings.EMBEDDING_DIM,
                embedding=vector,
            )
            .on_conflict_do_update(
                constraint="uq_candidate_embeddings_once_per_model",
                set_={
                    "embedding": vector,
                    "dim": settings.EMBEDDING_DIM,
                    "updated_at": select(CandidateEmbedding.updated_at),
                },
            )
        )
        await session.commit()

    log.info(
        "candidate_embedded",
        candidate_id=candidate_id,
        model=settings.EMBEDDING_MODEL,
        latency_ms=result.latency_ms,
    )


async def _candidate_text(
    session, tenant: uuid.UUID, candidate_id: uuid.UUID
) -> str:
    """The parsed CV text for a candidate, from R2 via the document's key.

    The newest parsed document wins, mirroring `parsed_text_keys` in
    `persist.py`: a candidate may have several CVs, and the one a recruiter
    uploaded last is the one that represents them now. An absent or unreadable
    document returns empty text, which the caller treats as "nothing to embed
    beyond the structured fields" rather than as an error.
    """
    row = (
        await session.execute(
            select(CandidateDocument.text_key)
            .where(
                CandidateDocument.tenant_id == tenant,
                CandidateDocument.candidate_id == candidate_id,
                CandidateDocument.parse_state == CandidateDocument.PARSED,
                CandidateDocument.text_key.is_not(None),
            )
            .order_by(CandidateDocument.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return ""
    (key,) = row
    store = body_store()
    body = await store.get(key)
    return body or ""
