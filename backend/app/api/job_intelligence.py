"""Start a Job Intelligence analysis, read it back, follow its state.

Two routes, both naming an `opportunity_id` and both gated by
`load_visible_opportunity` — the per-recruiter visibility rule the AST test in
`tests/test_opportunity_routes_guarded.py` enforces structurally on every
opportunity-touching route. RLS alone would hand a colleague another recruiter's
analysis; this gate keeps the two consistent.

POST starts the analysis by creating a `pending` row and enqueuing the arq job,
then returns 202 — the row exists but the answer does not. The three LLM calls
run in the worker, not here, for two reasons that are both load-bearing:

1. **The worker has the LLM provider; the api does not.** Extraction, classification
   and explanation all run in the worker because the LLM provider credentials live
   on the worker service. The api process has no provider configured, so a call here
   passes an empty `LLM_PROVIDER_BASE_URL` and fails the configured-check.
2. **Three model calls have no business inside an HTTP request** — the same
   argument `start_sourcing` makes.

GET reads the stored row back, in whatever state it is. `pending`/`running` is
"still working"; `done` carries the analysis; `failed` carries a sentence. The
panel polls GET until the row is terminal, exactly as the sourcing panel polls
a run. A job order with no analysis yet answers `{"intelligence": null}` with
200 — "no analysis" is a state of a job order that exists, and a 404 here would
be indistinguishable from another agency's id.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select, text

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.job_intelligence import JobIntelligence
from app.services.visibility import load_visible_opportunity, opportunity_chain_ids
from app.workers.job_intelligence_jobs import JOB_RUN_JOB_INTELLIGENCE
from app.workers.queue import enqueue

log = get_logger(__name__)

router = APIRouter(tags=["job_intelligence"])

# The spend gate: one atomic statement that performs the midnight-UTC date
# rollover AND the increment AND returns the spent count, so two concurrent
# POSTs serialize on the tenant row lock and neither undercounts. A counter
# on the tenant — not a COUNT(*) of the intelligence rows, because those
# POSTs are upserts: a re-run updates the existing row, and counting rows
# cannot count events that create no rows. A refused POST has already spent
# its increment, which is harmless — a refusal costs no model call, and the
# window resets at midnight.
# allow-hardcode: SQL statement, not a phrase list.
_SPEND_RUN = text(
    """
    UPDATE tenants SET
        llm_runs_count = CASE WHEN llm_runs_date = :today THEN llm_runs_count + 1
                              ELSE 1 END,
        llm_runs_date = :today
    WHERE id = :tenant_id
    RETURNING llm_runs_count
    """
)


async def _spend_analysis_run(session, tenant_id: uuid.UUID) -> None:
    """Charge this agency's daily analysis allowance, or refuse with 429."""
    spent = (
        await session.execute(
            _SPEND_RUN,
            {"today": datetime.now(UTC).date(), "tenant_id": tenant_id},
        )
    ).scalar_one()
    if spent > settings.INTELLIGENCE_DAILY_QUOTA:
        raise HTTPException(
            status_code=429,
            detail=(
                f"This agency has started its {settings.INTELLIGENCE_DAILY_QUOTA} "
                "analyses for today. Try again tomorrow."
            ),
        )

# allow-hardcode: a sentence shown to a recruiter, not configuration.
_ENQUEUE_FAILED = (
    "This analysis was created but could not be queued. Try again in a few minutes."
)


@router.post("/opportunities/{opportunity_id}/intelligence", status_code=202)
async def run_intelligence(request: Request, opportunity_id: uuid.UUID) -> dict:
    """Queue a Job Intelligence analysis for this job order.

    202, not 200: the row exists but the answer does not, exactly as
    `start_sourcing` answers. The worker does the three model calls.

    Every order is analysed, including a title-only one: the understand
    prompt's two-tier grounding makes a thin order produce a useful
    typical-role write-up at low confidence, so there is no pre-flight
    refusal to escape with a flag — the one-click contract is "Run analysis
    always runs".
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    row_id = uuid.uuid4()
    async with tenant_session(tenant_uuid) as session:
        # Load under the visibility guard before writing the row — a share
        # recipient may run the analysis on work shown to them, which is
        # visibility rather than edit rights (the edit-exemption in the AST
        # guard mirrors `start_sourcing`). Resolves supersede chains so the
        # analysis runs on the *current* revision's requirements.
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        opportunity_id = current.id

        # The spend gate: 4-5 model calls per analysis, bounded per agency per
        # day. Runs inside the tenant session and BEFORE the upsert, so a
        # refused POST writes nothing.
        await _spend_analysis_run(session, tenant_uuid)

        # Upsert: one row per opportunity. A re-run resets a finished row to
        # `pending` rather than accumulating a second row, matching the
        # synchronous version's contract — the button re-runs, it does not
        # append.
        existing = (
            await session.execute(
                select(JobIntelligence).where(
                    JobIntelligence.opportunity_id == opportunity_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                JobIntelligence(
                    id=row_id,
                    tenant_id=tenant_uuid,
                    opportunity_id=opportunity_id,
                    state=JobIntelligence.PENDING,
                    created_by=user_uuid,
                )
            )
        else:
            existing.state = JobIntelligence.PENDING
            existing.failure_reason = None
            existing.understanding = None
            existing.persona = None
            existing.search_plan = None
            # A re-run is a fresh attempt, not a continuation of the last
            # failed one. Without this, the attempts counter accumulates
            # across re-runs; once it passes `JOB_INTELLIGENCE_MAX_ATTEMPTS`
            # the job's conditional claim fails the row immediately with
            # "attempts exhausted" before ever calling the model — which is
            # how a user clicking Run Analysis again on a failed job order
            # got an instant failure with no error logged.
            existing.attempts = 0
            row_id = existing.id
        await session.commit()

    # Enqueued after the commit, because the job reads the row it is named for.
    # `queue_name` sends the analysis to the interactive queue so the dedicated
    # interactive worker picks it up regardless of how deep the background
    # replay/extraction backlog on the default queue is.
    if not await enqueue(
        JOB_RUN_JOB_INTELLIGENCE,
        queue_name=settings.ARQ_INTERACTIVE_QUEUE,
        tenant_id=str(tenant_uuid),
        opportunity_id=str(opportunity_id),
        row_id=str(row_id),
    ):
        log.warning("job_intelligence_enqueue_failed", row_id=str(row_id))
        async with tenant_session(tenant_uuid) as session:
            row = await session.get(JobIntelligence, row_id)
            if row is not None:
                row.state = JobIntelligence.FAILED
                row.failure_reason = _ENQUEUE_FAILED
                body = _serialize(row)
                await session.commit()
                return body

    async with tenant_session(tenant_uuid) as session:
        row = await session.get(JobIntelligence, row_id)
        return _serialize(row)


@router.get("/opportunities/{opportunity_id}/intelligence")
async def get_intelligence(request: Request, opportunity_id: uuid.UUID) -> dict:
    """Read the stored analysis, in whatever state it is in."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        current = await load_visible_opportunity(session, opportunity_id, user_uuid, role)
        # An analysis recorded against an earlier revision still belongs to
        # this job order, so the read covers every id in the chain. `id` breaks
        # the tie deterministically: two rows can exist if a re-run landed on a
        # new revision, and the panel wants the newest, not whichever row the
        # planner returns first.
        chain = await opportunity_chain_ids(session, current.id)
        row = (
            await session.execute(
                select(JobIntelligence)
                .where(JobIntelligence.opportunity_id.in_(chain))
                .order_by(JobIntelligence.created_at.desc(), JobIntelligence.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return {"intelligence": None}
        return _serialize(row)


def _serialize(row: JobIntelligence) -> dict:
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
    if row.state == JobIntelligence.DONE and row.understanding is not None:
        body["intelligence"] = {
            "understanding": row.understanding,
            "persona": row.persona,
            "search_plan": row.search_plan,
            "occupation": row.occupation,
        }
        body["removed_codes"] = row.removed_codes
    else:
        body["intelligence"] = None
    return body
