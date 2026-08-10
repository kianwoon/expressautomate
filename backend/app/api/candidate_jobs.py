"""The job orders that best fit one candidate — the reverse of sourcing.

Sourcing (`app/api/sourcing.py`) ranks an agency's candidates against ONE job
order and stores the run. This route ranks the agency's job orders against ONE
candidate and answers immediately: the candidate modal's "Find Job" asks
"where could I place this person?", a lighter question than "who fits this
role?", and one that needs no stored record — there is no model call, no quota
and no polling, because the score is the same pure arithmetic
`app/services/sourcing/score.py` uses, run over the rows already in memory.

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
from app.models.candidate import CandidateRole, CandidateSkill
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


@router.get("/candidates/{candidate_id}/jobs")
async def find_candidate_jobs(request: Request, candidate_id: uuid.UUID) -> dict:
    """The best-fitting visible job orders for one candidate, best first.

    Synchronous by design: every fact the scorer needs (the candidate's roles
    and skills, and each vacancy's title/skills/salary/company columns) is
    loaded in a handful of queries, and the scoring is arithmetic in Python —
    there is nothing to wait for and nothing worth storing.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    limit = settings.CANDIDATE_JOBS_TOP_N

    async with tenant_session(tenant_uuid) as session:
        # 404, never 403, exactly as every by-id candidate read behaves.
        candidate = await load_visible_candidate(session, candidate_id, user_uuid, role)

        roles = list(
            (
                await session.execute(
                    select(CandidateRole).where(CandidateRole.candidate_id == candidate_id)
                )
            )
            .scalars()
        )
        skills = list(
            (
                await session.execute(
                    select(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id)
                )
            )
            .scalars()
        )

        # What the recruiter can see, current revisions only — a superseded row
        # is the stale version of a vacancy whose successor is also here.
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

    # Whether the salary component can ever score for THIS candidate. The
    # scorer abstains when either side lacks a full record (amount, currency
    # and period), so when the candidate's expectation is incomplete the
    # absence is a candidate-level fact — every job order abstains for the
    # same reason, and the screen states it once rather than on every card.
    candidate_salary = None
    if (
        candidate.expected_salary is not None
        and candidate.salary_currency is not None
        and candidate.salary_period is not None
    ):
        candidate_salary = {
            "amount": float(candidate.expected_salary),
            "currency": candidate.salary_currency,
            "period": candidate.salary_period,
        }

    return {
        "items": [
            _serialize_job(
                opportunity,
                score=str(total),
                reasons=serialize_components(components),
            )
            for opportunity, total, components in shortlist
        ],
        # How many visible current revisions were examined, and how many of
        # them produced a score. The gap is the vacancies with nothing
        # comparable on record — reported so a shortlist that is shorter than
        # the database is an answer rather than a mystery.
        "considered": considered,
        "scored": len(scored),
        "limit": limit,
        "candidate_salary": candidate_salary,
    }
