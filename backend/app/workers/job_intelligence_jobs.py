"""The arq job that runs a Job Intelligence analysis for one job order.

Its own module for the reason `sourcing_jobs.py` and `cv_jobs.py` give:
`app/workers/jobs.py` is at the repo's 1500-line ceiling, and this job shares
nothing with mail ingestion but the queue it arrives on.

**Why a job, not a request.** The three LLM calls (understand → persona →
search) belong in the worker process, not the api process, for two reasons that
are both load-bearing:

1. **The worker has Cerebras; the api does not.** Extraction, classification
   and explanation all run here because the Cerebras credentials live on the
   worker service, not the api service. A request-handler call would pass an
   empty `CEREBRAS_BASE_URL`, fall back to OpenRouter, and 400 — which is
   exactly the failure the synchronous first cut shipped with.

2. **Three model calls have no business inside an HTTP request.** Same argument
   `start_sourcing` makes: the row exists but the answer does not.

**Failure discipline mirrors `run_sourcing`.** The row moves to `running`
before the pipeline (arq only reschedules on `Retry`, and nothing here raises
one), `attempts` is spent in the conditional claim so a job that crashes every
time reaches `failed`, and `rescan_stuck` re-enqueues rows stranded at
`pending` or `running` once the outage ends.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.job_intelligence import JobIntelligence
from app.models.opportunity import Opportunity
from app.models.opportunity_code import OpportunityCode
from app.services.job_intelligence.engine import analyze
from app.services.llm.client import LLMInvalidJSON

log = get_logger(__name__)

# The states a job may legitimately start from. `running` is included so a
# worker killed mid-analysis is picked up by `rescan_stuck` rather than
# stranded forever — the same reasoning as `sourcing_jobs._RESUMABLE`.
_RESUMABLE = (JobIntelligence.PENDING, JobIntelligence.RUNNING)

# The constant producers enqueue. Matches the string registered in
# `app.workers.settings.WorkerSettings.functions`; a mismatch fails on the far
# side of the queue, where the producer already saw success.
JOB_RUN_JOB_INTELLIGENCE = "run_job_intelligence"

# allow-hardcode: a sentence shown to a recruiter, not configuration.
_MODEL_FAILED = "The analysis could not be produced just now. Try again in a few minutes."
_NO_CONTEXT = "This job order has no title or description to analyse. Add one and try again."


async def run_job_intelligence(
    ctx,
    *,
    tenant_id: str,
    opportunity_id: str,
    row_id: str,
) -> None:
    """Run the three-stage analysis and store it on the row.

    Carries its tenant, like every job here: background work has no request and
    therefore no session tenant. A job naming a mismatched (tenant, row) pair
    reads no row under the tenant policy and quietly does nothing.
    """
    tenant = uuid.UUID(tenant_id)
    opportunity_key = uuid.UUID(opportunity_id)
    record = uuid.UUID(row_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(select(JobIntelligence).where(JobIntelligence.id == record))
        ).scalar_one_or_none()
        if row is None:
            # Unknown row, or a job whose tenant does not own it. RLS already
            # decided; there is nothing to do and nothing to report.
            log.info("job_intelligence_skipped_unknown_row", row_id=row_id)
            return

        # Conditional claim, not read-then-write: between the two, `rescan_stuck`
        # could re-enqueue this row while the first attempt still runs, and a
        # blind write would let both proceed to the pipeline. Restating the
        # state in the WHERE makes check and write one statement; the loser
        # matches no row. `attempts` is spent here so a job that crashes every
        # time reaches `failed` rather than looping forever, one model call
        # per sweep.
        claimed = (
            await session.execute(
                update(JobIntelligence)
                .where(
                    JobIntelligence.id == record,
                    JobIntelligence.state.in_(_RESUMABLE),
                )
                .values(state=JobIntelligence.RUNNING, attempts=JobIntelligence.attempts + 1)
                .returning(JobIntelligence.attempts)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if claimed is None:
            log.info("job_intelligence_skipped_claimed_elsewhere", row_id=row_id)
            return
        (attempts,) = claimed
        await session.commit()

    if attempts > settings.JOB_INTELLIGENCE_MAX_ATTEMPTS:
        # Terminal, so `rescan_stuck` stops seeing it. Claimed first and refused
        # second on purpose: leaving it at `pending` would let the sweep pick it
        # up again and discover the same thing — the loop, not the end of it.
        log.warning("job_intelligence_attempts_exhausted", row_id=row_id, attempts=attempts)
        await _fail(tenant, record, _MODEL_FAILED)
        return

    async with tenant_session(tenant) as session:
        opportunity = (
            await session.execute(select(Opportunity).where(Opportunity.id == opportunity_key))
        ).scalar_one_or_none()
        if opportunity is None:
            log.info("job_intelligence_opportunity_missing", row_id=row_id)
            await _fail(tenant, record, _MODEL_FAILED)
            return
        codes = list(
            (
                await session.execute(
                    select(OpportunityCode).where(
                        OpportunityCode.opportunity_id == opportunity_key
                    )
                )
            ).scalars()
        )

    try:
        outcome = await analyze(opportunity, codes)
    except (LLMInvalidJSON, Exception) as exc:
        # A bad model answer, or a transport failure reaching Cerebras. Either
        # is a failed run the recruiter can retry; neither is retried here,
        # because temperature zero makes a plain retry the same answer twice.
        log.warning(
            "job_intelligence_failed",
            row_id=row_id,
            opportunity_id=opportunity_id,
            error=repr(exc),
        )
        await _fail(tenant, record, _MODEL_FAILED)
        return

    if not outcome.result.understanding.role:
        # Empty context produces an empty understanding. Record it as failed
        # with an actionable reason rather than storing a blank analysis.
        await _fail(tenant, record, _NO_CONTEXT)
        return

    result = outcome.result
    now = datetime.now(UTC)
    async with tenant_session(tenant) as session:
        await session.execute(
            update(JobIntelligence)
            .where(JobIntelligence.id == record)
            .values(
                state=JobIntelligence.DONE,
                understanding=result.understanding.model_dump(mode="json"),
                persona=result.persona.model_dump(mode="json"),
                search_plan=result.search_plan.model_dump(mode="json"),
                model_name=outcome.stats.model,
                prompt_tokens=outcome.stats.prompt_tokens,
                completion_tokens=outcome.stats.completion_tokens,
                latency_ms=outcome.stats.latency_ms,
                removed_codes=outcome.removed_codes,
                failure_reason=None,
                analysed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    log.info(
        "job_intelligence_done",
        row_id=row_id,
        opportunity_id=opportunity_id,
        latency_ms=outcome.stats.latency_ms,
    )


async def _fail(tenant: uuid.UUID, record: uuid.UUID, reason: str) -> None:
    """Mark a row failed with a sentence a recruiter can act on."""
    async with tenant_session(tenant) as session:
        await session.execute(
            update(JobIntelligence)
            .where(JobIntelligence.id == record)
            .values(state=JobIntelligence.FAILED, failure_reason=reason)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
