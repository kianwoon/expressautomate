"""Run and read back a Job Intelligence analysis for one job order.

Two routes, both naming an `opportunity_id` and both gated by
`load_visible_opportunity` — the per-recruiter visibility rule the AST test in
`tests/test_opportunity_routes_guarded.py` enforces structurally on every
opportunity-touching route. RLS alone would hand a colleague another recruiter's
analysis; this gate keeps the two consistent.

POST runs the analysis synchronously. The three LLM calls are fast (Cerebras,
~1.5s each) and on-demand — a recruiter clicks "Job Intelligence" and waits for
the result, the same interaction model as editing a field. A background queue
would add a polling round-trip for no gain at this latency, so Phase 1 keeps it
synchronous. The row is upserted, so a re-click replaces the previous analysis
rather than accumulating rows.

GET reads the stored analysis back, answering `{"intelligence": null}` with 200
when none exists yet — "no analysis" is a state of a job order that exists, and
a 404 here would be indistinguishable from another agency's id.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.job_intelligence import JobIntelligence
from app.models.opportunity_code import OpportunityCode
from app.services.job_intelligence.engine import analyze
from app.services.llm.client import LLMInvalidJSON
from app.services.visibility import load_visible_opportunity

log = get_logger(__name__)

router = APIRouter(tags=["job_intelligence"])

# allow-hardcode: a sentence shown to a recruiter, not configuration.
_NO_CONTEXT = (
    "This job order has no title or description to analyse. Add one and try again."
)
# allow-hardcode: as above.
_NOT_CONFIGURED = (
    "Job Intelligence is not configured on this deployment. Ask your administrator."
)
# allow-hardcode: as above.
_MODEL_FAILED = (
    "The analysis could not be produced just now. Try again in a few minutes."
)


@router.post("/opportunities/{opportunity_id}/intelligence")
async def run_intelligence(request: Request, opportunity_id: uuid.UUID) -> dict:
    """Run the three-stage analysis and store it, replacing any prior one."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    model_id = settings.JOB_INTELLIGENCE_MODEL or settings.EXTRACTION_MODEL_FAST
    if not settings.cerebras_configured(model_id):
        raise HTTPException(status_code=503, detail=_NOT_CONFIGURED)

    async with tenant_session(tenant_uuid) as session:
        opportunity = await load_visible_opportunity(
            session, opportunity_id, user_uuid, role
        )
        codes = (
            (
                await session.execute(
                    select(OpportunityCode).where(
                        OpportunityCode.opportunity_id == opportunity_id
                    )
                )
            )
            .scalars()
            .all()
        )
        # `opportunity` stays readable after the session closes because
        # `SessionLocal` is `expire_on_commit=False`: the column attributes are
        # already loaded and never re-fetched. The LLM call runs outside the
        # session so it cannot hold a transaction open across a network hop.

    try:
        outcome = await analyze(opportunity, codes)
    except LLMInvalidJSON as exc:
        log.warning("job_intelligence_failed", opportunity_id=str(opportunity_id), error=repr(exc))
        raise HTTPException(status_code=502, detail=_MODEL_FAILED) from exc

    if not outcome.result.understanding.role:
        # Empty context produces an empty understanding. Tell the recruiter what
        # to fix rather than storing a blank analysis they cannot act on.
        raise HTTPException(status_code=409, detail=_NO_CONTEXT)

    result = outcome.result
    intelligence = _serialize(
        result.understanding.model_dump(),
        result.persona.model_dump(),
        result.search_plan.model_dump(),
    )

    now = datetime.now(UTC)
    async with tenant_session(tenant_uuid) as session:
        await _upsert(
            session,
            tenant_uuid,
            opportunity_id,
            user_uuid,
            understanding=result.understanding.model_dump(mode="json"),
            persona=result.persona.model_dump(mode="json"),
            search_plan=result.search_plan.model_dump(mode="json"),
            model_name=outcome.stats.model,
            prompt_tokens=outcome.stats.prompt_tokens,
            completion_tokens=outcome.stats.completion_tokens,
            latency_ms=outcome.stats.latency_ms,
            removed_codes=outcome.removed_codes,
            analysed_at=now,
        )
        await session.commit()

    # Same shape as GET: `removed_codes` and `analysed_at` sit beside
    # `intelligence`, so a re-run and a read render identically on the client.
    return {
        "intelligence": intelligence,
        "removed_codes": outcome.removed_codes,
        "analysed_at": now.isoformat(),
    }


@router.get("/opportunities/{opportunity_id}/intelligence")
async def get_intelligence(request: Request, opportunity_id: uuid.UUID) -> dict:
    """Read the stored analysis, or `{"intelligence": null}` if none yet."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        row = (
            await session.execute(
                select(JobIntelligence).where(
                    JobIntelligence.opportunity_id == opportunity_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return {"intelligence": None}
        return {
            "intelligence": _serialize(
                row.understanding, row.persona, row.search_plan
            ),
            "removed_codes": row.removed_codes,
            "analysed_at": row.analysed_at.isoformat() if row.analysed_at else None,
        }


def _serialize(understanding: dict, persona: dict, search_plan: dict) -> dict:
    """The shape both POST and GET return, so a re-run and a read match."""
    return {
        "understanding": understanding,
        "persona": persona,
        "search_plan": search_plan,
    }


async def _upsert(
    session,
    tenant_uuid: uuid.UUID,
    opportunity_id: uuid.UUID,
    user_uuid: uuid.UUID,
    *,
    understanding: dict,
    persona: dict,
    search_plan: dict,
    model_name: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    removed_codes: list[str],
    analysed_at: datetime,
) -> None:
    """Insert or update the one row for this opportunity.

    The unique constraint on `(tenant_id, opportunity_id)` makes this an
    UPDATE on a re-run. Loaded under RLS so a foreign id is simply not found,
    the same 404-by-absence every other read here relies on. `analysed_at` is
    passed in rather than computed here so the response and the stored row agree
    on the same instant.
    """
    row = (
        await session.execute(
            select(JobIntelligence).where(
                JobIntelligence.opportunity_id == opportunity_id
            )
        )
    ).scalar_one_or_none()
    fields = dict(
        understanding=understanding,
        persona=persona,
        search_plan=search_plan,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        removed_codes=removed_codes,
        analysed_at=analysed_at,
    )
    if row is None:
        session.add(
            JobIntelligence(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                opportunity_id=opportunity_id,
                created_by=user_uuid,
                **fields,
            )
        )
    else:
        for key, value in fields.items():
            setattr(row, key, value)
