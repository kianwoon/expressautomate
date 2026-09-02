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

**Results are persisted at the terminal read.** The career bot's retention
turned out to be short — a task polled at 14:28 was a 404 on their side the
same afternoon — so the passthrough design left a completed panel pointing
at an expired task. `get_external_search_status` and
`get_external_search_results` write the ranked list onto the membership row
when a read finds the task completed (and `failed`/`paused` set
`finished_at` too, so a dead search is never re-shown as fresh), and
`GET …/external-candidates/latest` returns the newest search row for the
opportunity — results from us when the career bot has already forgotten the
task. The source-provenance rule in CLAUDE.md applies to the stored copy:
each result carries its own evidence (`source`, `source_url`,
`match_reason`, `credibility`), and the row's retention is the job order's.
"""

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

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
STATUS_NOT_PROVISIONED = "not_provisioned"

# SQLSTATE 42P01: the local membership table is absent. It shipped with the
# feature's migration, and CI runs that migration on every api deploy — but
# 2026-09-02 proved the chain is not unbreakable (a failed build followed by
# frontend-only fixes let api ship the code without the migration). A missing
# table is a deployment fault an operator must fix (run `alembic upgrade
# head`), so it gets its own sentence rather than a 500 with a stack trace.

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


# SQLSTATE class 42 = insufficient schema / undefined object.
_MISSING_TABLE = "42P01"


def _not_provisioned_body(exc: Exception) -> dict[str, Any]:
    """The structured answer for a database missing the feature's tables.

    One catch around the whole database-touching half of the route: the
    failure today (2026-09-02) surfaced as a 500 with a 120-line stack
    trace for `relation "external_candidate_searches" does not exist` —
    that sentence is exactly what the panel should have shown instead.
    """
    log.error("external_candidates_schema_missing", error=str(exc))
    return {
        "status": STATUS_NOT_PROVISIONED,
        "task_id": None,
        "message": (
            "The external candidate search is not provisioned in this "
            "deployment's database yet — an administrator needs to run the "
            "pending migrations."
        ),
    }


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
    becomes `exclude` and the single plan platform becomes `platforms[]` —
    the list form the revised spec prefers (the legacy `platform` scalar is
    the same as a one-element `platforms[]`, so this changes nothing today
    while keeping the door open for multi-platform plans). Empty/None fields
    are dropped rather than sent as nulls — the career bot treats an absent
    field and a null the same, and a shorter request is an honest one.
    """
    payload: dict[str, Any] = {}
    queries = plan.get("queries")
    if isinstance(queries, list) and queries:
        payload["queries"] = [q for q in queries if isinstance(q, str) and q.strip()]
    negative = plan.get("negative_queries")
    if isinstance(negative, list) and negative:
        payload["exclude"] = [n for n in negative if isinstance(n, str) and n.strip()]
    for source, target in (
        ("platform", "platforms"),
        ("location", "location"),
        ("salary", "salary"),
        ("employment_type", "employment_type"),
    ):
        value = plan.get(source)
        if isinstance(value, str) and value.strip():
            if target == "platforms":
                payload[target] = [value.strip()]
            else:
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
        try:
            current = await load_visible_opportunity(
                session, opportunity_id, user_uuid, role
            )
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
        except DBAPIError as exc:
            if (
                exc.orig is not None
                and getattr(exc.orig, "sqlstate", None) == _MISSING_TABLE
            ):
                # The schema predates the feature — most likely the whole
                # membership table is absent, so there is nothing started to
                # speak of. Structured, not a 500. The rollback + expunge
                # first: the `tenant_session` context manager commits AGAIN
                # on the way out, and a session still holding the failed
                # INSERT would re-raise the same fault there — a 500 after
                # this handler had already returned cleanly. Expunging
                # leaves the exit commit a clean no-op.
                session.expunge_all()
                await session.rollback()
                return _not_provisioned_body(exc)
            raise

    return {"status": "started", "task_id": started.task_id, "message": None}


class _TaskGate:
    """The membership check every per-task route runs before calling out.

    A task id is honoured only when a row WE wrote says it belongs to an
    opportunity the caller can see. The row lookup goes through
    `tenant_session`, so RLS scopes the tenant before the query runs, and
    `load_visible_opportunity` scopes the recruiter on top — a colleague's
    unshared job order is not visible, so neither are its external searches.

    `check()` keeps the row it vouched for: the terminal read writes the
    results onto exactly that row, so persistence can never attach a career
    bot's answer to a row other than the one that authorised the read.
    """

    def __init__(self, session, opportunity_id: uuid.UUID, task_id: str) -> None:
        self._session = session
        self._opportunity_id = opportunity_id
        self._task_id = task_id
        self.row: ExternalCandidateSearch | None = None

    async def check(self) -> ExternalCandidateSearch:
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
        self.row = row
        return row


# Terminal career-bot task states. `completed` is the only one with results
# worth keeping; `failed`/`paused` still get `finished_at` so a dead search
# is never re-presented as if it were still fresh.
_TERMINAL_TASK_STATUSES = {"completed", "failed", "paused"}


async def _persist_terminal(
    tenant_uuid: uuid.UUID, row: ExternalCandidateSearch, task_status: str
) -> None:
    """Write the terminal state of one search onto its row, idempotently.

    The row arrives detached — the read that authorised the career-bot call
    ran in its own `tenant_session`, now closed — so this re-reads the row in
    a session of its own, re-checks `finished_at` there (the guard that makes
    a second terminal read a no-op even across requests), sets the terminal
    columns and commits. For a `completed` task the ranked list is fetched
    again through the client, so the status poll and the results read cannot
    disagree about what was stored.

    A failure here must not fail the read: the route already has the career
    bot's answer in hand, and a persistence miss degrades to exactly the
    pre-persistence behaviour (results shown now, lost on expiry) — not to a
    500 the recruiter cannot act on. The session is created inside the try
    for the same reason: an unavailable database is a persistence miss, not
    an error page.
    """
    if task_status not in _TERMINAL_TASK_STATUSES:
        return
    try:
        async with tenant_session(tenant_uuid) as session:
            fresh = await session.get(ExternalCandidateSearch, row.id)
            if fresh is None or fresh.finished_at is not None:
                return
            if task_status == "completed":
                body = await _client().get_results(fresh.task_id)
                fresh.results = body.get("results") or []
            fresh.finished_at = dt.datetime.now(dt.UTC)
            await session.commit()
    except Exception:
        log.warning("external_search_results_persist_failed", task_id=row.task_id)


async def _latest_search_row(
    session, opportunity_id: uuid.UUID
) -> ExternalCandidateSearch | None:
    """The newest search row for this opportunity — the finished search a
    returning visitor sees, whatever the career bot still remembers."""
    return (
        await session.execute(
            select(ExternalCandidateSearch)
            .where(ExternalCandidateSearch.opportunity_id == opportunity_id)
            # Finished searches first (newest), then in-flight ones by when
            # they were started — the search a returning visitor sees is the
            # one that most recently mattered.
            .order_by(
                ExternalCandidateSearch.finished_at.desc().nulls_last(),
                ExternalCandidateSearch.created_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


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
        gate = _TaskGate(session, current.id, task_id)
        row = await gate.check()

    try:
        task = await _client().get_task(task_id)
    except CareerBotUnreachableError as exc:
        return _unreachable_body(exc)
    except CareerBotError as exc:
        return _refusal(exc)

    # The terminal state lands on the row the same read authorised — a
    # completed poll stores the results, so a panel that only ever polls
    # status still leaves the search readable after the career bot expires
    # the task.
    await _persist_terminal(tenant_uuid, row, task.status)

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
        gate = _TaskGate(session, current.id, task_id)
        row = await gate.check()

    try:
        body = await _client().get_results(task_id)
    except CareerBotUnreachableError as exc:
        return _unreachable_body(exc)
    except CareerBotError as exc:
        return _refusal(exc)

    # A completed results read is the terminal event of a search — store it
    # on the row the read was authorised against, so the panel that showed
    # these candidates can show them again tomorrow.
    body_status = body.get("status")
    if isinstance(body_status, str):
        await _persist_terminal(tenant_uuid, row, body_status)

    return {
        "status": "ok",
        "task_id": body.get("task_id", task_id),
        "task_status": body.get("status"),
        "summary": body.get("summary"),
        "results": body.get("results") or [],
        "message": None,
    }


@router.get("/opportunities/{opportunity_id}/external-candidates/latest")
async def get_latest_external_search(
    request: Request, opportunity_id: uuid.UUID
) -> dict:
    """The newest search for this job order — what a returning visitor sees.

    Opening the job order again must show the candidates the last search
    found, not an empty tab, and the career bot's own retention is too short
    to rely on (a task started at 14:28 was a 404 on their side the same
    afternoon). The route answers from OUR row: the saved ranked list when
    the search finished, its career-bot task status when it is still in
    flight, and `none` when no search has ever run — the panel offers a
    fresh search in every one of those cases, plus re-polls through the
    per-task routes when the saved row says the task is still working.

    200 even when the answer is `none`: "no search yet" is the state of the
    tab, not an error, the same reading `get_intelligence` takes when it
    answers `{"intelligence": None}`.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    if not settings.career_bot_configured():
        return {
            "status": STATUS_UNCONFIGURED,
            "search": None,
            "message": "External candidate search is not set up for this deployment.",
        }

    async with tenant_session(tenant_uuid) as session:
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        row = await _latest_search_row(session, current.id)
        if row is None:
            return {"status": "none", "search": None, "message": None}
        return {
            "status": "ok",
            "search": _search_payload(row),
            "message": None,
        }


def _search_payload(row: ExternalCandidateSearch) -> dict[str, Any]:
    """The saved-search shape the panel consumes — the row's own facts, plus
    `results` as the career bot's verbatim ranked list."""
    return {
        "task_id": row.task_id,
        "task_status": row_status_of(row),
        "results": row.results or [],
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def row_status_of(row: ExternalCandidateSearch) -> str | None:
    """The task status a saved row can honestly claim.

    A finished row keeps the terminal status it finished with — `completed`
    with results, `failed` or `paused` without — and an unfinished row is a
    task still working on the career bot's side, so the panel keeps polling
    it through the per-task routes exactly as it did while the tab was open.
    """
    if row.finished_at is None:
        return "running"
    if row.results:
        return "completed"
    return "failed"

