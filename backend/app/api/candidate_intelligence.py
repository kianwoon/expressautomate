"""Start a Candidate Intelligence analysis, read it back, follow its state.

Mirrors `job_intelligence.py` end to end: two routes, both naming a
`candidate_id` and both gated by `load_visible_candidate` — the per-recruiter
visibility rule the AST test in `tests/test_candidate_routes_guarded.py`
enforces structurally on every candidate-touching route. RLS alone would hand a
colleague another recruiter's analysis; this gate keeps the two consistent.

POST starts the analysis by creating a `pending` row and enqueuing the arq job,
then returns 202 — the row exists but the answer does not. The five LLM calls
run in the worker, not here, for the same two load-bearing reasons as Job
Intelligence:

1. **The worker has Cerebras; the api does not.** Every extraction, classification
   and intelligence call runs in the worker because the Cerebras credentials
   live on the worker service. The api process has only OpenRouter, so a call
   here passes an empty `CEREBRAS_BASE_URL`, falls back to OpenRouter, and 400s.
2. **Five model calls have no business inside an HTTP request.**

GET reads the stored row back, in whatever state it is. `pending`/`running` is
"still working"; `done` carries the analysis; `failed` carries a sentence. The
panel polls GET until the row is terminal, exactly as the Job Intelligence panel
polls. A candidate with no analysis yet answers `{"intelligence": null}` with
200 — "no analysis" is a state of a candidate that exists, and a 404 here would
be indistinguishable from another agency's id.
"""

import uuid

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.auth import _require_session_with_role
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate_intelligence import CandidateIntelligence
from app.services.visibility import load_visible_candidate
from app.workers.candidate_intelligence_jobs import JOB_RUN_CANDIDATE_INTELLIGENCE
from app.workers.queue import enqueue

log = get_logger(__name__)

router = APIRouter(tags=["candidate_intelligence"])

# allow-hardcode: a sentence shown to a recruiter, not configuration.
_ENQUEUE_FAILED = (
    "This analysis was created but could not be queued. Try again in a few minutes."
)


@router.post("/candidates/{candidate_id}/intelligence", status_code=202)
async def run_candidate_intelligence_route(
    request: Request, candidate_id: uuid.UUID
) -> dict:
    """Queue a Candidate Intelligence analysis for this candidate.

    202, not 200: the row exists but the answer does not, exactly as the Job
    Intelligence POST answers. The worker does the five Cerebras calls.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    row_id = uuid.uuid4()
    async with tenant_session(tenant_uuid) as session:
        # Load under the visibility guard before writing the row — a share
        # recipient may run the analysis on a candidate shown to them, which is
        # visibility rather than edit rights (the same rule the Job Intelligence
        # route applies via `load_visible_opportunity`).
        await load_visible_candidate(session, candidate_id, user_uuid, role)

        # Upsert: one row per candidate. A re-run resets a finished row to
        # `pending` rather than accumulating a second row, matching the Job
        # Intelligence contract — the button re-runs, it does not append.
        existing = (
            await session.execute(
                select(CandidateIntelligence).where(
                    CandidateIntelligence.candidate_id == candidate_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                CandidateIntelligence(
                    id=row_id,
                    tenant_id=tenant_uuid,
                    candidate_id=candidate_id,
                    state=CandidateIntelligence.PENDING,
                    created_by=user_uuid,
                )
            )
        else:
            existing.state = CandidateIntelligence.PENDING
            existing.failure_reason = None
            existing.work = None
            existing.assessment = None
            row_id = existing.id
        await session.commit()

    # Enqueued after the commit, because the job reads the row it is named for.
    if not await enqueue(
        JOB_RUN_CANDIDATE_INTELLIGENCE,
        tenant_id=str(tenant_uuid),
        candidate_id=str(candidate_id),
        row_id=str(row_id),
    ):
        log.warning("candidate_intelligence_enqueue_failed", row_id=str(row_id))
        async with tenant_session(tenant_uuid) as session:
            row = await session.get(CandidateIntelligence, row_id)
            if row is not None:
                row.state = CandidateIntelligence.FAILED
                row.failure_reason = _ENQUEUE_FAILED
                body = _serialize(row)
                await session.commit()
                return body

    async with tenant_session(tenant_uuid) as session:
        row = await session.get(CandidateIntelligence, row_id)
        return _serialize(row)


@router.get("/candidates/{candidate_id}/intelligence")
async def get_candidate_intelligence_route(
    request: Request, candidate_id: uuid.UUID
) -> dict:
    """Read the stored analysis, in whatever state it is in."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        await load_visible_candidate(session, candidate_id, user_uuid, role)
        row = (
            await session.execute(
                select(CandidateIntelligence).where(
                    CandidateIntelligence.candidate_id == candidate_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return {"intelligence": None}
        return _serialize(row)


def _serialize(row: CandidateIntelligence) -> dict:
    """The shape both POST and GET return: state, then the analysis if done.

    `intelligence` is `null` until the row is `done` — the panel renders the
    state for `pending`/`running`/`failed` and the analysis only on success.
    """
    body: dict = {
        "id": str(row.id),
        "state": row.state,
        "failure_reason": row.failure_reason,
        "analysed_at": row.analysed_at.isoformat() if row.analysed_at else None,
    }
    if row.state == CandidateIntelligence.DONE and row.work is not None:
        body["intelligence"] = {
            "work": row.work,
            "assessment": row.assessment,
        }
    else:
        body["intelligence"] = None
    return body
