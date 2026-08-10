"""The job orders that best fit one candidate — the reverse of sourcing.

Sourcing (`app/api/sourcing.py`) ranks an agency's candidates against ONE job
order and stores the run. This module ranks the agency's job orders against ONE
candidate and keeps the answer: the candidate modal's "Find Job" asks "where
could I place this person?", and the Jobs tab reopens to the last result
without re-scoring.

Two routes, mirroring `candidate_intelligence.py`'s shape:

- `POST /candidates/{candidate_id}/jobs` — run the matcher and upsert the
  shortlist snapshot (`CandidateJobShortlist`, one row per candidate), so a
  re-run replaces rather than appends. Synchronous by design: every fact the
  scorer needs is loaded in a handful of queries and the scoring is arithmetic
  in Python — there is nothing to wait for and nothing worth a background job.
- `GET /candidates/{candidate_id}/jobs` — read the stored snapshot back. When
  Find Job has never run for this candidate, it answers the same shape with
  `saved_at: null`, so "not run yet" is distinguishable from "ran, no matches".

The scoring contract is the same one `score_candidate` gives the sourcing run:

- **Absent is not bad.** A job order with nothing comparable to the
  candidate's record on a component reports `raw=null` and is left out of the
  total; the breakdown shows the note instead of a silent zero.
- **Knowing nothing is not a score.** A job order with no comparable component
  at all (`total is None`) is dropped entirely, never shown as "0%".
- The **semantic** component always abstains here. The forward direction
  embeds one job order and compares it against stored CV embeddings; the
  reverse has no stored job-order embeddings to compare against, and embedding
  every visible vacancy inside a request would turn a modal lookup into a
  model spend. The six structured components (title, skills, employer, salary,
  tenure, recency) are what "based on the candidate profile" means.

Tenant and recruiter boundaries follow the rest of the API: the candidate is
loaded through `load_visible_candidate` and the job orders through
`visible_opportunities`, so another agency's rows are a 404 and a colleague's
unshared job order is simply not considered — the shortlist is drawn from what
this recruiter can already see. Superseded revisions are excluded in favour of
their current replacement, exactly as the job-orders list reads.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateRole, CandidateSkill
from app.models.candidate_jobs import CandidateJobShortlist
from app.models.opportunity import Opportunity
from app.services.sourcing.persist import serialize_components
from app.services.sourcing.score import score_candidate
from app.services.visibility import load_visible_candidate, visible_opportunities

router = APIRouter(tags=["candidate-jobs"])

# The stored `review_status` word and the word the job-orders list shows.
# Mirrors `opportunities.py`'s translation: the pipeline writes `ready`, the
# UI reads `new`, and a row that arrives already translated must pass through
# rather than be renamed twice.
_STORED_TO_FILTER = {"ready": "new", "needs_review": "needs_review", "reviewed": "reviewed"}


def _today() -> date:
    """Today in UTC, passed to the scorer so a run is reproducible."""
    return datetime.now(UTC).date()


def _candidate_salary(candidate: Candidate) -> dict | None:
    """The candidate's salary expectation, when complete.

    The scorer abstains when any of the three is missing, so when the
    candidate's own expectation is incomplete the salary absence is a
    candidate-level fact — every job order abstains for the same reason, and
    the screen states it once rather than on every card. `None` here is what
    lets the screen say that.
    """
    if (
        candidate.expected_salary is None
        or candidate.salary_currency is None
        or candidate.salary_period is None
    ):
        return None
    return {
        "amount": float(candidate.expected_salary),
        "currency": candidate.salary_currency,
        "period": candidate.salary_period,
    }


def _serialize_job(row: Opportunity, score: str, reasons: list[dict]) -> dict:
    """One shortlisted job order, with enough of the record to render a card.

    Deliberately lighter than `opportunities._payload`: the candidate modal
    shows why the vacancy fits, not the full provenance the job-orders panel
    needs, so the join-heavy select that builds that payload is not worth the
    two aliases and the lateral here. `_raw` strings stay `_raw`, exactly as
    the list returns them — the recruiter recognises what the email said.
    """
    return {
        "id": str(row.id),
        "company_name_raw": row.company_name_raw,
        "job_title_raw": row.job_title_raw,
        "location_raw": row.location_raw,
        "salary_raw": row.salary_raw,
        "salary_min": None if row.salary_min is None else float(row.salary_min),
        "salary_max": None if row.salary_max is None else float(row.salary_max),
        "salary_currency": row.salary_currency,
        "salary_period": row.salary_period,
        "working_hours_raw": row.working_hours_raw,
        "duration_raw": row.duration_raw,
        "requirements": row.requirements,
        "employment_type": row.employment_type,
        "assigned_user_id": str(row.assigned_user_id) if row.assigned_user_id else None,
        "review_status": _STORED_TO_FILTER.get(row.review_status, row.review_status),
        "quality_state": row.quality_state,
        "received_datetime": (
            row.received_datetime.isoformat() if row.received_datetime else None
        ),
        "score": score,
        # The numbers behind the score, as strings from the NUMERIC columns —
        # the same wire shape `sourcing` sends, for the same reason: a float
        # round-trip would show 0.6499999999999999 for a value the scorer
        # computed exactly.
        "reasons": reasons,
    }


async def _score_shortlist(
    session, candidate: Candidate, user_uuid: uuid.UUID, role: str, limit: int
) -> tuple[list[dict], int, int, dict | None]:
    """Score every visible current revision against the candidate.

    Returns the serialized shortlist (best first), how many vacancies were
    examined, how many of them produced a score, and the candidate's complete
    salary expectation (or None). Pure arithmetic over rows already loaded —
    this is the whole cost of a Find Job run.
    """
    roles = list(
        (
            await session.execute(
                select(CandidateRole).where(CandidateRole.candidate_id == candidate.id)
            )
        )
        .scalars()
    )
    skills = list(
        (
            await session.execute(
                select(CandidateSkill).where(CandidateSkill.candidate_id == candidate.id)
            )
        )
        .scalars()
    )

    # What the recruiter can see, current revisions only — a superseded row is
    # the stale version of a vacancy whose successor is also here.
    rows = (
        await session.execute(
            select(Opportunity)
            .where(visible_opportunities(user_uuid, role))
            .where(Opportunity.superseded_by_opportunity_id.is_(None))
        )
    ).scalars()

    today = _today()
    scored: list[tuple[Opportunity, Decimal, list]] = []
    considered = 0
    for opportunity in rows:
        considered += 1
        total, components = score_candidate(
            opportunity,
            candidate,
            roles,
            skills,
            # No semantic scores: there is no stored job-order embedding to
            # compare against, so the semantic component abstains — see the
            # module docstring.
            semantic_scores=None,
            today=today,
        )
        if total is None:
            # Nothing about this vacancy was comparable to this candidate —
            # the "knowing nothing is not a score" rule. Dropped, never
            # rendered as 0%.
            continue
        scored.append((opportunity, total, components))

    # Best first, then id, so two equal-scoring vacancies come back in the
    # same order every time — the tie-break the sourcing run uses.
    scored.sort(key=lambda item: (-item[1], str(item[0].id)))
    shortlist = scored[:limit]

    items = [
        _serialize_job(
            opportunity,
            score=str(total),
            reasons=serialize_components(components),
        )
        for opportunity, total, components in shortlist
    ]
    return items, considered, len(scored), _candidate_salary(candidate)


def _view(row: CandidateJobShortlist | None, limit: int) -> dict:
    """The wire shape GET and POST both answer.

    `saved_at: null` means Find Job has never run for this candidate — the
    Jobs tab then shows its "No shortlist yet" state, which is a different
    thing from "ran, nothing matched".
    """
    if row is None:
        return {
            "items": [],
            "considered": 0,
            "scored": 0,
            "limit": limit,
            "candidate_salary": None,
            "saved_at": None,
        }
    return {
        "items": row.items,
        "considered": row.considered,
        "scored": row.scored,
        "limit": row.limit,
        "candidate_salary": row.candidate_salary,
        "saved_at": row.updated_at.isoformat(),
    }


@router.post("/candidates/{candidate_id}/jobs")
async def run_candidate_jobs(request: Request, candidate_id: uuid.UUID) -> dict:
    """Run Find Job for one candidate and save the result.

    Upserts the snapshot on `(tenant_id, candidate_id)` — one row per
    candidate, so the Jobs tab always reopens to the *latest* run and a
    recruiter who edits the profile can re-run to replace it.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    limit = settings.CANDIDATE_JOBS_TOP_N

    async with tenant_session(tenant_uuid) as session:
        # 404, never 403, exactly as every by-id candidate read behaves.
        candidate = await load_visible_candidate(session, candidate_id, user_uuid, role)

        items, considered, scored, candidate_salary = await _score_shortlist(
            session, candidate, user_uuid, role, limit
        )

        row = (
            await session.execute(
                select(CandidateJobShortlist).where(
                    CandidateJobShortlist.candidate_id == candidate_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                CandidateJobShortlist(
                    tenant_id=tenant_uuid,
                    candidate_id=candidate_id,
                    created_by=user_uuid,
                    considered=considered,
                    scored=scored,
                    limit=limit,
                    candidate_salary=candidate_salary,
                    items=items,
                )
            )
        else:
            # Re-run replaces the snapshot. `created_by` stays the first
            # runner, not the latest — the row answers "what was shown", and
            # the person who first asked is the person who owns the tab.
            row.considered = considered
            row.scored = scored
            row.limit = limit
            row.candidate_salary = candidate_salary
            row.items = items
        await session.commit()

    # Re-read in a fresh session, the same shape `candidate_intelligence` uses
    # after its upsert: the commit ended the transaction (and with it the
    # transaction-local tenant setting), and the trigger-set `updated_at` only
    # exists server-side — a stale in-memory row would read a value that was
    # never written.
    async with tenant_session(tenant_uuid) as session:
        fresh = (
            await session.execute(
                select(CandidateJobShortlist).where(
                    CandidateJobShortlist.candidate_id == candidate_id
                )
            )
        ).scalar_one()
        return _view(fresh, limit)


@router.get("/candidates/{candidate_id}/jobs")
async def get_candidate_jobs(request: Request, candidate_id: uuid.UUID) -> dict:
    """The last saved Find Job shortlist for one candidate, or the never-run
    empty shape when Find Job has not been run for them yet."""
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    limit = settings.CANDIDATE_JOBS_TOP_N

    async with tenant_session(tenant_uuid) as session:
        # The 404 guard still applies: a candidate another recruiter holds is
        # not this recruiter's to read the shortlist of either.
        await load_visible_candidate(session, candidate_id, user_uuid, role)
        row = (
            await session.execute(
                select(CandidateJobShortlist).where(
                    CandidateJobShortlist.candidate_id == candidate_id
                )
            )
        ).scalar_one_or_none()

    return _view(row, limit)
