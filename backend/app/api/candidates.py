"""The agency's candidate list.

Nothing here is AI-derived. Every value was typed by a person or came from a
spreadsheet a person uploaded, so there is no confidence, no evidence, and no
review queue — only records and the people who edited them.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, or_, select

from app.api.auth import _require_session
from app.core.config import settings
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateFieldOverride, CandidateSkill

router = APIRouter(tags=["candidates"])

StageFilter = Literal["new", "contacted", "submitted", "placed", "rejected"]
RecordStatusFilter = Literal["active", "archived", "merged"]


def _serialize(candidate: Candidate) -> dict:
    return {
        "id": str(candidate.id),
        "full_name": candidate.full_name,
        "email": candidate.email,
        "phone_raw": candidate.phone_raw,
        "phone_e164": candidate.phone_e164,
        "current_title": candidate.current_title,
        "current_employer": candidate.current_employer,
        "location": candidate.location,
        "years_experience": candidate.years_experience,
        "expected_salary": (
            float(candidate.expected_salary) if candidate.expected_salary is not None else None
        ),
        "salary_currency": candidate.salary_currency,
        "salary_period": candidate.salary_period,
        "available_from": (
            candidate.available_from.isoformat() if candidate.available_from else None
        ),
        "notice_period_raw": candidate.notice_period_raw,
        "employment_type": candidate.employment_type,
        "notes": candidate.notes,
        "pipeline_stage": candidate.pipeline_stage,
        "record_status": candidate.record_status,
        "merged_into_candidate_id": (
            str(candidate.merged_into_candidate_id)
            if candidate.merged_into_candidate_id
            else None
        ),
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
    }


@router.get("/candidates")
async def list_candidates(
    request: Request,
    # Resolved in the body, not the signature: a default bound at import would
    # freeze the setting at the value it had when the module loaded.
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    pipeline_stage: StageFilter | None = None,
    record_status: RecordStatusFilter | None = None,
    q: str | None = None,
) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    ceiling = settings.CANDIDATES_PAGE_LIMIT
    page_limit = ceiling if limit is None else min(limit, ceiling)

    async with tenant_session(tenant_uuid) as session:
        # Counted over the whole tenant, before any filter or window, so a
        # chip does not change meaning as the recruiter pages.
        counts = {"all": 0}
        for stage, n in await session.execute(
            select(Candidate.pipeline_stage, func.count())
            .where(Candidate.record_status != Candidate.MERGED)
            .group_by(Candidate.pipeline_stage)
        ):
            counts["all"] += n
            counts[stage] = counts.get(stage, 0) + n

        # Merged rows are hidden by default — a merged record is not a person
        # any more — but an explicit filter must still reach them. A merged
        # row's pointer runs loser → survivor, so there is no link from the
        # survivor back, and without this filter a wrongly merged person is
        # invisible and `unmerge` is reachable only by curl.
        if record_status is not None:
            base = select(Candidate).where(Candidate.record_status == record_status)
        else:
            base = select(Candidate).where(Candidate.record_status != Candidate.MERGED)
        if pipeline_stage is not None:
            base = base.where(Candidate.pipeline_stage == pipeline_stage)
        if q:
            # Name, email and phone: the three things a recruiter has to hand
            # when they are looking for somebody they spoke to last week.
            # Escape LIKE metacharacters (%, _) with backslash so they are treated
            # as literals, not as SQL wildcards. This is not a SQL-injection concern—the
            # value is parameterized—but without escaping, a recruiter searching for
            # a literal "%" or "_" gets wrong results.
            normalized = q.strip().lower()
            escaped = (
                normalized.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            like = f"%{escaped}%"
            base = base.where(
                or_(
                    func.lower(Candidate.full_name).like(like, escape="\\"),
                    func.lower(Candidate.email).like(like, escape="\\"),
                    Candidate.phone_e164.like(like, escape="\\"),
                    Candidate.phone_raw.like(like, escape="\\"),
                )
            )

        total = (
            await session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    base.order_by(Candidate.updated_at.desc()).limit(page_limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )

    return {
        "items": [_serialize(c) for c in rows],
        "total": total,
        "limit": page_limit,
        "offset": offset,
        "counts": counts,
    }


@router.get("/candidates/{candidate_id}")
async def get_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        skills = (
            (
                await session.execute(
                    select(CandidateSkill)
                    .where(CandidateSkill.candidate_id == candidate_id)
                    .order_by(CandidateSkill.skill_normalized)
                )
            )
            .scalars()
            .all()
        )
        overrides = (
            (
                await session.execute(
                    select(CandidateFieldOverride.field_name).where(
                        CandidateFieldOverride.candidate_id == candidate_id
                    )
                )
            )
            .scalars()
            .all()
        )

    payload = _serialize(candidate)
    payload["skills"] = [s.skill for s in skills]
    # So the UI can say why an import did not change a field, rather than
    # leaving the recruiter to conclude the import is broken.
    payload["overridden_fields"] = sorted(overrides)
    return payload


async def _load(session, candidate_id: uuid.UUID) -> Candidate:
    """Fetch inside the tenant session, so another agency's id is a 404.

    Not a 403: telling a caller that an id exists but is not theirs is itself
    a cross-tenant disclosure.
    """
    candidate = (
        await session.execute(select(Candidate).where(Candidate.id == candidate_id))
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate
