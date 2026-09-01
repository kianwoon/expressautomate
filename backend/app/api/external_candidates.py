"""External candidate search — the career bot, driven by the search plan.

Three routes, all naming an `opportunity_id` and all gated by
`load_visible_opportunity`, for the same reason `job_intelligence.py` is
(the AST test in `tests/test_opportunity_routes_guarded.py` enforces the
read guard structurally; the POST takes the read-guard-only exemption
`run_intelligence` takes, because a share recipient may run a search on
work shown to them — visibility, not edit rights).

**Why this is a proxy rather than the browser calling the career bot
directly.** The API key is a shared agency credential; handing it to every
browser would publish it. Three of its behaviours follow from being a proxy:

1. **The search plan comes from the job intelligence row, not the request.**
   The feature is "relying on info published in the search tab" — the plan
   is read from the newest `job_intelligence` row across the supersede
   chain, the same read GET /intelligence does, and the request body is
   empty. The browser never supplies queries, so an external search cannot
   be pointed at criteria the agency's own analysis never produced.
2. **Every task id is checked against a local row** (`external_candidate_searches`,
   written at start). The career bot's task ids are polled through our shared
   key; a recruiter who guessed another agency's task id must not read its
   results through us (§18).
3. **The career bot being down must not 500 the API** (CLAUDE.md). Every
   route answers a dead service with a structured status the panel renders
   as a sentence — `unreachable`, or the service's own refusal text bounded
   to 500 chars.

The career bot is async by design (spec: start → poll every ~5s → results),
so POST returns 202 with the task id, exactly as `start_sourcing` answers.
"""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.external_candidate_search import ExternalCandidateSearch
from app.models.job_intelligence import JobIntelligence
from app.services import career_bot
from app.services.career_bot import (
    CareerBotError,
    CareerBotRateLimited,
    CareerBotUnreachableError,
)
from app.services.visibility import load_visible_opportunity, opportunity_chain_ids

log = get_logger(__name__)

router = APIRouter(tags=["external_candidates"])

# Structured statuses the panel renders as sentences — the same shape the WA
# gateway routes answer with instead of a 500. None of these are written to
# the database; they are facts about *this request*, not about any row.
STATUS_UNCONFIGURED = "unconfigured"
STATUS_UNREACHABLE = "unreachable"
STATUS_REFUSED = "refused"

# The fallback when the career bot sent no quotable reason. Its own wording
# is preferred (§15) and `_MAX_MESSAGE_CHARS` bounds whatever arrived.
_REFUSED_FALLBACK = "The external candidate search service refused the search."


def _refusal(exc: CareerBotError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": STATUS_REFUSED,
        "task_id": None,
        "message": exc.message.strip() or _REFUSED_FALLBACK,
    }
    if isinstance(exc, CareerBotRateLimited) and exc.retry_after is not None:
        body["retry_after_seconds"] = exc.retry_after
    return body


def _unreachable_body(exc: CareerBotUnreachableError) -> dict[str, Any]:
    return {"status": STATUS_UNREACHABLE, "task_id": None, "message": exc.message}


def _client() -> career_bot.CareerBotClient:
    return career_bot.CareerBotClient()


async def _load_search_plan(
    session, opportunity_id: uuid.UUID
) -> tuple[dict[str, Any] | None, uuid.UUID | None]:
    """The newest search plan across the supersede chain, and the row's own
    opportunity id — `(None, None)` when no analysis has produced a plan yet.

    The same read `get_intelligence` does, narrowed to `search_plan` only: a
    re-run on a newer revision resets `search_plan` to null on that row, but
    an older revision's plan still describes this vacancy until the fresh
    analysis lands — which is exactly what "relying on info published in the
    search tab" means while a re-run is in flight.
    """
    chain = await opportunity_chain_ids(session, opportunity_id)
    row = (
        await session.execute(
            select(JobIntelligence)
            .where(JobIntelligence.opportunity_id.in_(chain))
            .order_by(JobIntelligence.created_at.desc(), JobIntelligence.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None, None
    plan = row.search_plan if isinstance(row.search_plan, dict) else None
    return plan, row.opportunity_id


def _payload_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Map the SearchPlan the search tab shows into the career bot's body
    (spec §2). Field names travel as-is where they match; `negative_queries`
    becomes `exclude`. Empty/None fields are dropped rather than sent as
    nulls — the career bot treats an absent field and a null the same, and
    a shorter request is an honest one.
    """
    payload: dict[str, Any] = {}
    queries = plan.get("queries")
    if isinstance(queries, list) and queries:
        payload["queries"] = [q for q in queries if isinstance(q, str) and q.strip()]
    negative = plan.get("negative_queries")
    if isinstance(negative, list) and negative:
        payload["exclude"] = [n for n in negative if isinstance(n, str) and n.strip()]
    for source, target in (
        ("platform", "platform"),
        ("location", "location"),
        ("salary", "salary"),
        ("employment_type", "employment_type"),
    ):
        value = plan.get(source)
        if isinstance(value, str) and value.strip():
            payload[target] = value.strip()
    return payload


@router.post(
    "/opportunities/{opportunity_id}/external-candidates/search", status_code=202
)
async def start_external_search(request: Request, opportunity_id: uuid.UUID) -> dict:
    """Start one external candidate search for this job order.

    202, not 200: the search takes minutes on the career bot side and the
    panel polls `GET …/{task_id}` — the same contract `start_sourcing`
    answers. Refuses with a structured status (never a 500) when the
    integration is not configured, the service is unreachable, or the
    service refuses the search; the panel renders `message` as the sentence.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    if not settings.career_bot_configured():
        # Unset URL/key is a real deployment state (the GRAPH_BASE_URL and
        # R2_* outage precedent in CLAUDE.md), answered here rather than
        # surfacing as an httpx error with no host.
        return {
            "status": STATUS_UNCONFIGURED,
            "task_id": None,
            "message": "External candidate search is not set up for this deployment.",
        }

    async with tenant_session(tenant_uuid) as session:
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        plan, plan_opportunity_id = await _load_search_plan(session, current.id)
        payload = _payload_from_plan(plan) if plan else {}
        if not payload.get("queries"):
            # 409, not 404: the job order exists and is visible — what is
            # missing is a precondition of the action (an analysis with a
            # search plan), the same reading `get_intelligence` takes when
            # it answers `{"intelligence": None}` with 200 instead of 404.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Run the job analysis first — external search uses the "
                    "search plan from the Search tab."
                ),
            )

        search_id = uuid.uuid4()
        try:
            started = await _client().start_search(payload)
        except CareerBotUnreachableError as exc:
            log.warning("career_bot_start_unreachable", error=exc.message)
            return _unreachable_body(exc)
        except CareerBotError as exc:
            log.warning(
                "career_bot_start_refused", status=exc.status, message=exc.message
            )
            return _refusal(exc)

        # The local membership row, written only after the career bot
        # accepted the search: a task id we could not start has no row, and
        # a row we could not start has no task id.
        session.add(
            ExternalCandidateSearch(
                id=search_id,
                tenant_id=tenant_uuid,
                opportunity_id=plan_opportunity_id or current.id,
                task_id=started.task_id,
                created_by=user_uuid,
            )
        )
        await session.commit()

    return {"status": "started", "task_id": started.task_id, "message": None}


class _TaskGate:
    """The membership check every per-task route runs before calling out.

    A task id is honoured only when a row WE wrote says it belongs to an
    opportunity the caller can see. The row lookup goes through
    `tenant_session`, so RLS scopes the tenant before the query runs, and
    `load_visible_opportunity` scopes the recruiter on top — a colleague's
    unshared job order is not visible, so neither are its external searches.
    """

    def __init__(self, session, opportunity_id: uuid.UUID, task_id: str) -> None:
        self._session = session
        self._opportunity_id = opportunity_id
        self._task_id = task_id

    async def check(self) -> None:
        row = (
            await self._session.execute(
                select(ExternalCandidateSearch).where(
                    ExternalCandidateSearch.task_id == self._task_id,
                    ExternalCandidateSearch.opportunity_id == self._opportunity_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            # Same answer for "no such task" and "not yours" — a 404 that
            # cannot be probed for which task ids exist.
            raise HTTPException(status_code=404, detail="Search not found.")


@router.get("/opportunities/{opportunity_id}/external-candidates/search/{task_id}")
async def get_external_search_status(
    request: Request, opportunity_id: uuid.UUID, task_id: str
) -> dict:
    """The poll the panel drives — the career bot's task status, verbatim."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    if not settings.career_bot_configured():
        return {
            "status": STATUS_UNCONFIGURED,
            "task_status": None,
            "message": "External candidate search is not set up for this deployment.",
        }

    async with tenant_session(tenant_uuid) as session:
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        await _TaskGate(session, current.id, task_id).check()

    try:
        task = await _client().get_task(task_id)
    except CareerBotUnreachableError as exc:
        return _unreachable_body(exc)
    except CareerBotError as exc:
        return _refusal(exc)

    return {
        "status": "polled",
        "task_status": task.status,
        "error": task.error,
        "message": None,
    }


@router.get(
    "/opportunities/{opportunity_id}/external-candidates/search/{task_id}/results"
)
async def get_external_search_results(
    request: Request, opportunity_id: uuid.UUID, task_id: str
) -> dict:
    """The ranked list. Passed through as `results` — the shape is the
    career bot's spec (§4), mirrored field by field would be a second copy
    of it that drifts."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    if not settings.career_bot_configured():
        return {
            "status": STATUS_UNCONFIGURED,
            "results": None,
            "message": "External candidate search is not set up for this deployment.",
        }

    async with tenant_session(tenant_uuid) as session:
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        await _TaskGate(session, current.id, task_id).check()

    try:
        body = await _client().get_results(task_id)
    except CareerBotUnreachableError as exc:
        return _unreachable_body(exc)
    except CareerBotError as exc:
        return _refusal(exc)

    return {
        "status": "ok",
        "task_id": body.get("task_id", task_id),
        "task_status": body.get("status"),
        "summary": body.get("summary"),
        "results": body.get("results") or [],
        "message": None,
    }

