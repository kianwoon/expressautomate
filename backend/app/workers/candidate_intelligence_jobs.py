"""The arq job that runs a Candidate Intelligence analysis for one candidate.

Mirrors `job_intelligence_jobs.py` for the same reasons that module exists:

1. **The worker has the LLM provider; the api does not.** The five LLM calls (history →
   automation → benchmark → gaps → residual) belong in the worker process, where
   the LLM provider credentials live. A request-handler call would pass an empty
   `LLM_PROVIDER_BASE_URL`, fall back to OpenRouter, and 400.

2. **Five model calls have no business inside an HTTP request** — the row
   exists but the answer does not, exactly as `run_job_intelligence` and
   `run_sourcing` answer.

**Failure discipline mirrors `run_job_intelligence`.** The row moves to
`running` before the pipeline, `attempts` is spent in the conditional claim so a
job that crashes every time reaches `failed`, and `rescan_stuck` re-enqueues
rows stranded at `pending` or `running` once the outage ends.

The one difference from `run_job_intelligence` is the input source: a job order
lives in a JSONB column on `opportunities`, but a candidate's CV text lives in
R2 at the parsed document's `text_key`. The newest `parsed` document's text is
fetched the same way `embedding_jobs._candidate_text` fetches it, and an absent
or empty CV fails the run with an actionable reason rather than analysing
nothing.
"""

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateDocument, CandidateRole, CandidateSkill
from app.models.candidate_intelligence import CandidateIntelligence
from app.services.candidate_intelligence.engine import analyze_candidate
from app.services.llm.client import LLMInvalidJSON, LLMNoContent
from app.services.storage.r2 import R2BodyStore

log = get_logger(__name__)

# The states a job may legitimately start from. `running` is included so a
# worker killed mid-analysis is picked up by `rescan_stuck` rather than
# stranded forever — the same reasoning as `job_intelligence_jobs._RESUMABLE`.
_RESUMABLE = (CandidateIntelligence.PENDING, CandidateIntelligence.RUNNING)

# The constant producers enqueue. Matches the string registered in
# `app.workers.settings.WorkerSettings.functions`; a mismatch fails on the far
# side of the queue, where the producer already saw success.
JOB_RUN_CANDIDATE_INTELLIGENCE = "run_candidate_intelligence"

# allow-hardcode: sentences shown to a recruiter, not configuration.
_MODEL_FAILED = "The analysis could not be produced just now. Try again in a few minutes."
_NO_CV = (
    "This candidate has no parsed CV to analyse. Upload one and try again once it has parsed."
)


async def run_candidate_intelligence(
    ctx,
    *,
    tenant_id: str,
    candidate_id: str,
    row_id: str,
) -> None:
    """Run the five-stage analysis and store it on the row.

    Carries its tenant, like every job here: background work has no request and
    therefore no session tenant. A job naming a mismatched (tenant, row) pair
    reads no row under the tenant policy and quietly does nothing.
    """
    tenant = uuid.UUID(tenant_id)
    candidate_key = uuid.UUID(candidate_id)
    record = uuid.UUID(row_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                select(CandidateIntelligence).where(CandidateIntelligence.id == record)
            )
        ).scalar_one_or_none()
        if row is None:
            # Unknown row, or a job whose tenant does not own it. RLS already
            # decided; there is nothing to do and nothing to report.
            log.info("candidate_intelligence_skipped_unknown_row", row_id=row_id)
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
                update(CandidateIntelligence)
                .where(
                    CandidateIntelligence.id == record,
                    CandidateIntelligence.state.in_(_RESUMABLE),
                )
                .values(
                    state=CandidateIntelligence.RUNNING,
                    attempts=CandidateIntelligence.attempts + 1,
                )
                .returning(CandidateIntelligence.attempts)
                .execution_options(synchronize_session=False)
            )
        ).first()
        if claimed is None:
            log.info("candidate_intelligence_skipped_claimed_elsewhere", row_id=row_id)
            return
        (attempts,) = claimed
        await session.commit()

    if attempts > settings.CANDIDATE_INTELLIGENCE_MAX_ATTEMPTS:
        # Terminal, so `rescan_stuck` stops seeing it. Claimed first and refused
        # second on purpose: leaving it at `pending` would let the sweep pick it
        # up again and discover the same thing — the loop, not the end of it.
        log.warning(
            "candidate_intelligence_attempts_exhausted", row_id=row_id, attempts=attempts
        )
        await _fail(tenant, record, _MODEL_FAILED)
        return

    async with tenant_session(tenant) as session:
        candidate = await session.get(Candidate, candidate_key)
        if candidate is None:
            log.info("candidate_intelligence_candidate_missing", row_id=row_id)
            await _fail(tenant, record, _MODEL_FAILED)
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
        cv_text = await _candidate_text(session, tenant, candidate_key)

    if not cv_text.strip():
        # No parsed CV: there is nothing to analyse. Record it as failed with
        # an actionable reason rather than storing an empty analysis.
        await _fail(tenant, record, _NO_CV)
        return

    try:
        outcome = await _analyze_with_no_content_retry(candidate, roles, skills, cv_text)
    except Exception as exc:  # noqa: BLE001 — a terminal failure, never a retry
        # A bad model answer, or a transport failure reaching the LLM provider. Either
        # is a failed run the recruiter can retry; neither is retried here,
        # because temperature zero makes a plain retry the same answer twice.
        # `LLMNoContent` is the one exception to that rule and is handled
        # inside `_analyze_with_no_content_retry` — an empty response is not an
        # answer, so re-asking is a different request, not the same answer.
        log.warning(
            "candidate_intelligence_failed",
            row_id=row_id,
            candidate_id=candidate_id,
            error=repr(exc),
        )
        await _fail(tenant, record, _MODEL_FAILED)
        return

    result = outcome.result
    now = datetime.now(UTC)
    async with tenant_session(tenant) as session:
        await session.execute(
            update(CandidateIntelligence)
            .where(CandidateIntelligence.id == record)
            .values(
                state=CandidateIntelligence.DONE,
                work=result.work.model_dump(mode="json"),
                assessment=result.assessment.model_dump(mode="json"),
                model_name=outcome.stats.model,
                prompt_tokens=outcome.stats.prompt_tokens,
                completion_tokens=outcome.stats.completion_tokens,
                latency_ms=outcome.stats.latency_ms,
                failure_reason=None,
                analysed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    log.info(
        "candidate_intelligence_done",
        row_id=row_id,
        candidate_id=candidate_id,
        latency_ms=outcome.stats.latency_ms,
    )


async def _fail(tenant: uuid.UUID, record: uuid.UUID, reason: str) -> None:
    """Mark a row failed with a sentence a recruiter can act on."""
    async with tenant_session(tenant) as session:
        await session.execute(
            update(CandidateIntelligence)
            .where(CandidateIntelligence.id == record)
            .values(state=CandidateIntelligence.FAILED, failure_reason=reason)
            .execution_options(synchronize_session=False)
        )
        await session.commit()


async def _analyze_with_no_content_retry(candidate, roles, skills, cv_text: str):
    """Run the pipeline, re-asking after an empty or unparseable response.

    `LLMNoContent` means the model spent its budget thinking and never emitted
    an answer — re-asking is a different request, not "the same answer twice",
    so it gets a bounded retry (the model's reasoning trace is sampled even at
    temperature zero, which thinking mode ignores anyway).

    `LLMInvalidJSON` gets the same retry now that the provider is GLM's coding
    plan: the model wraps answers in an envelope that is inconsistent (dict,
    JSON string, malformed string, bare list) and the `_parse` layer rescues
    most shapes but not all. Re-asking is materially different — the model's
    envelope choice and reasoning trace change each call (production log
    2026-08-25: `expected an object, got list` on the work pass). Any other
    failure — a transport error — propagates immediately to the caller's
    single fail path.
    """
    retries = settings.CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRIES
    delay = settings.CANDIDATE_INTELLIGENCE_NO_CONTENT_RETRY_DELAY_SECONDS
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return await analyze_candidate(candidate, roles, skills, cv_text)
        except (LLMNoContent, LLMInvalidJSON) as exc:
            last_error = exc
            if attempt < retries:
                log.warning(
                    "candidate_intelligence_retry",
                    attempt=attempt + 1,
                    retries=retries,
                    kind=type(exc).__name__,
                )
                await asyncio.sleep(delay)
    raise last_error  # type: ignore[misc]  # retries >= 0 guarantees one attempt


async def _candidate_text(session, tenant: uuid.UUID, candidate_id: uuid.UUID) -> str:
    """The parsed CV text for a candidate, from R2 via the document's key.

    The newest parsed document wins, mirroring `embedding_jobs._candidate_text`
    and `parsed_text_keys` in `persist.py`: a candidate may have several CVs,
    and the one a recruiter uploaded last is the one that represents them now.
    An absent or unreadable document returns empty text, which the caller treats
    as "no CV to analyse".
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
    store = R2BodyStore()
    body = await store.get(key)
    return body or ""
