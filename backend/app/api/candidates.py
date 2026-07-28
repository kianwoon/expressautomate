"""The agency's candidate list.

Nothing here is AI-derived. Every value was typed by a person or came from a
spreadsheet a person uploaded, so there is no confidence, no evidence, and no
review queue — only records and the people who edited them.
"""

import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.api.auth import _require_session
from app.core.config import settings
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateFieldOverride, CandidateSkill
from app.models.tenant import User
from app.services.candidate_matching import find_candidate
from app.services.candidate_naming import (
    is_matchable_phone,
    normalize_email,
    normalize_phone,
    normalize_skill,
)

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


async def _lock_pair(session, first: uuid.UUID, second: uuid.UUID) -> None:
    """Take a row lock on both candidates, lowest id first.

    One statement per row rather than an `IN (...)` with an ORDER BY: the order
    in which Postgres locks the rows of a single statement is a property of the
    chosen plan, not of the ORDER BY, so only separate statements make the
    ordering something this code actually decides. A missing row simply locks
    nothing — the 404 comes from `_load` afterwards.
    """
    for candidate_id in sorted((first, second), key=lambda value: value.bytes):
        await session.execute(
            select(Candidate.id).where(Candidate.id == candidate_id).with_for_update()
        )


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


# The widest value `candidates.expected_salary` can physically hold, derived
# from the column rather than written out: a Numeric(p, s) stores up to
# 10**(p - s) - 10**-s. Anything larger is a numeric overflow the database
# raises as a DBAPIError, which reaches the client as a 500 — so the bound is
# enforced here, at the schema boundary, and a migration that widens the column
# moves this bound with it instead of leaving a stale literal behind.
_SALARY_TYPE = Candidate.__table__.c.expected_salary.type
MAX_EXPECTED_SALARY = float(
    10 ** (_SALARY_TYPE.precision - _SALARY_TYPE.scale) - 10 ** -_SALARY_TYPE.scale
)

# Postgres SQLSTATE for a unique/exclusion violation. Only this class of
# integrity error means "somebody already has that"; every other one (a CHECK,
# a foreign key) is a different fault and must not be dressed up as a duplicate.
_UNIQUE_VIOLATION = "23505"


def _is_duplicate(exc: IntegrityError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION


class _CandidateFieldRules:
    """Validation shared by the create and patch bodies.

    Every rule here exists because the database would otherwise answer for it,
    and the database's answer is the wrong one to show a recruiter: a blank
    name trips a CHECK and used to surface as 409 "Already recorded", and an
    over-large salary overflowed the column into a 500.
    """

    @field_validator("full_name", check_fields=False)
    @classmethod
    def _name_is_not_blank(cls, value: str | None) -> str | None:
        # `ck_candidates_name_not_blank` says the same thing in the database.
        # Saying it here means the caller is told which field is wrong.
        # A field validator only runs for a value the caller actually sent, so
        # None here means an explicit `"full_name": null` on a PATCH — which
        # the NOT NULL column would refuse anyway, as a 500.
        if value is None or not value.strip():
            raise ValueError("full_name must not be blank")
        return value

    @field_validator("email", check_fields=False)
    @classmethod
    def _email_is_parseable(cls, value: str | None) -> str | None:
        """An absent email is fine; an unparseable one is not.

        `normalize_email` returns None for both, and storing None for the
        second is silent data loss on the field the matcher depends on — the
        recruiter believes they recorded an identity key and nothing did.
        """
        if value is None:
            return None
        normalized = normalize_email(value)
        if normalized is None:
            raise ValueError("email is not a valid address")
        return normalized

    @field_validator("phone_raw", check_fields=False)
    @classmethod
    def _phone_is_parseable(cls, value: str | None) -> str | None:
        """Same distinction as email, one step weaker.

        A number that parses but belongs to a switchboard is deliberately kept
        (see `_identity_phone`) — it is a real number that simply never
        identifies anyone. A number that does not parse at all is a typo, and
        accepting it would leave the record with no usable phone while looking
        as though it had one.
        """
        if value is None:
            return None
        if normalize_phone(value) is None:
            raise ValueError("phone_raw is not a valid phone number")
        return value

    @field_validator("expected_salary", check_fields=False)
    @classmethod
    def _salary_fits_the_column(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if value < 0 or value > MAX_EXPECTED_SALARY:
            raise ValueError(
                f"expected_salary must be between 0 and {MAX_EXPECTED_SALARY}"
            )
        return value

    @field_validator("years_experience", check_fields=False)
    @classmethod
    def _years_are_plausible(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0 or value > settings.CANDIDATE_MAX_YEARS_EXPERIENCE:
            raise ValueError(
                "years_experience must be between 0 and "
                f"{settings.CANDIDATE_MAX_YEARS_EXPERIENCE}"
            )
        return value


class CandidateIn(_CandidateFieldRules, BaseModel):
    """Only `full_name` is required.

    A recruiter frequently has a name and a phone number and nothing else, and
    a form that refused that would be a form they work around.
    """

    full_name: str
    email: str | None = None
    phone_raw: str | None = None
    current_title: str | None = None
    current_employer: str | None = None
    location: str | None = None
    years_experience: int | None = None
    expected_salary: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    available_from: date | None = None
    notice_period_raw: str | None = None
    employment_type: str | None = None
    notes: str | None = None
    pipeline_stage: StageFilter | None = None
    skills: list[str] | None = None


class CandidateUpdate(_CandidateFieldRules, BaseModel):
    """Every field optional — this is a PATCH.

    Reusing `CandidateIn` here would be a bug: its `full_name` is required, so
    `PATCH {"current_title": "..."}` would be rejected 422 for omitting a field
    the caller never intended to change. `exclude_unset=True` on the dump is
    what makes "not sent" different from "set to null", and that distinction
    only exists if the model allows the field to be absent.
    """

    full_name: str | None = None
    email: str | None = None
    phone_raw: str | None = None
    current_title: str | None = None
    current_employer: str | None = None
    location: str | None = None
    years_experience: int | None = None
    expected_salary: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    available_from: date | None = None
    notice_period_raw: str | None = None
    employment_type: str | None = None
    notes: str | None = None
    pipeline_stage: StageFilter | None = None
    skills: list[str] | None = None


class MergeRequest(BaseModel):
    target_id: uuid.UUID


# Fields a human edit protects from a later import. `skills` is excluded: it is
# a set, not a value, and merging an imported skill into a curated list loses
# nothing.
_OVERRIDABLE = (
    "full_name", "email", "phone_raw", "current_title", "current_employer",
    "location", "years_experience", "expected_salary", "salary_currency",
    "salary_period", "available_from", "notice_period_raw", "employment_type",
    "notes",
)


@router.post("/candidates", status_code=201)
async def create_candidate(request: Request, body: CandidateIn) -> dict:
    user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        phone_e164 = _identity_phone(body.phone_raw)
        email = normalize_email(body.email)

        match = await find_candidate(session, tenant_uuid, email, phone_e164)
        if match.conflict is not None:
            # Two different people. Attaching to either would put one person's
            # details on the other's record.
            raise HTTPException(
                status_code=409,
                detail=(
                    "This email and phone belong to two different candidates "
                    f"({match.conflict[0]} and {match.conflict[1]}). "
                    "Merge them first, or correct the details."
                ),
            )
        if match.candidate_id is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Already recorded as candidate {match.candidate_id}",
            )

        candidate_id = uuid.uuid4()
        values = body.model_dump(exclude={"skills"})
        values.update(
            id=candidate_id,
            tenant_id=tenant_uuid,
            email=email,
            phone_e164=phone_e164,
            pipeline_stage=body.pipeline_stage or "new",
            record_status=Candidate.ACTIVE,
            created_by=user_uuid,
            updated_by=user_uuid,
        )
        try:
            await session.execute(insert(Candidate).values(**values))
            await _replace_skills(session, tenant_uuid, candidate_id, body.skills or [])
            await session.commit()
        except IntegrityError as exc:
            # The unique indexes are the backstop for a race the matcher's
            # read could not see. A 409 says the same thing the matcher would
            # have; a 500 would blame the recruiter for a collision.
            #
            # Only a unique violation means that. Any other constraint failing
            # here is a bug in this endpoint's validation, and reporting it as
            # a duplicate sends someone hunting for a conflicting record that
            # does not exist — so it is re-raised rather than disguised.
            await session.rollback()
            if not _is_duplicate(exc):
                raise
            raise HTTPException(status_code=409, detail="Already recorded") from exc

    return await get_candidate(request, candidate_id)


def _comparable(field: str, value: object) -> object:
    """Normalize a field's value to the form it is compared and stored in.

    `expected_salary` arrives as `Decimal` from the column and `float` from a
    PATCH body; `available_from` arrives as `date` from the column and from
    the body. Both sides must go through the same cast or an unchanged value
    would look changed and record a meaningless override.
    """
    if value is None:
        return None
    if field == "expected_salary":
        return float(value)
    if field == "available_from":
        return value.isoformat()
    return value


@router.patch("/candidates/{candidate_id}")
async def update_candidate(
    request: Request, candidate_id: uuid.UUID, body: CandidateUpdate
) -> dict:
    user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        # A merged row is not a person any more; its identity belongs to the
        # target. Editing one writes to a record nothing reads, and worse, it
        # can change the email and phone that `unmerge` has to give back.
        # Archive already refuses a merged row for the same reason.
        if candidate.record_status == Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the candidate first")
        values = body.model_dump(exclude={"skills"}, exclude_unset=True)
        if "phone_raw" in values:
            values["phone_e164"] = _identity_phone(values["phone_raw"])
        if "email" in values:
            values["email"] = normalize_email(values["email"])

        # Only a field whose incoming value actually differs from what the
        # row currently holds is a human decision worth protecting. Recording
        # every field present in the body — even one the client merely
        # echoed back unchanged — would mark the whole record "edited by
        # hand" and freeze it from every later import, for this endpoint and
        # for the phase-2 import worker alike, since both write through here.
        changed_fields = [
            field
            for field in _OVERRIDABLE
            if field in values
            and _comparable(field, values[field]) != _comparable(field, getattr(candidate, field))
        ]

        values["updated_by"] = user_uuid

        try:
            await session.execute(
                update(Candidate).where(Candidate.id == candidate_id).values(**values)
            )
        except IntegrityError as exc:
            # Editing an email or phone to one somebody else already holds.
            # The unique index is right to refuse; a 500 would tell the
            # recruiter the app is broken when their data is merely ambiguous.
            # Narrowed to a unique violation: any other constraint failing here
            # is this endpoint's own validation gap, not a duplicate.
            await session.rollback()
            if not _is_duplicate(exc):
                raise
            raise HTTPException(
                status_code=409,
                detail="Another candidate already has that email or phone",
            ) from exc
        # A field that actually changed is remembered as a human decision.
        # Without this a later import of a stale sheet silently undoes the
        # correction, and nothing in the data afterwards could say it
        # happened.
        for field in changed_fields:
            await session.execute(
                pg_insert(CandidateFieldOverride)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_uuid,
                    candidate_id=candidate_id,
                    field_name=field,
                    human_value=None if values[field] is None else str(values[field]),
                    changed_by=user_uuid,
                )
                .on_conflict_do_update(
                    constraint="uq_candidate_overrides_one_per_field",
                    set_={
                        "human_value": (
                            None if values[field] is None else str(values[field])
                        ),
                        "changed_by": user_uuid,
                    },
                )
            )
        if body.skills is not None:
            await _replace_skills(session, tenant_uuid, candidate_id, body.skills)
        await session.commit()

    return await get_candidate(request, candidate_id)


@router.post("/candidates/{candidate_id}/archive")
async def archive_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        if candidate.record_status == Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the candidate first")
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(record_status=Candidate.ARCHIVED)
        )
        await session.commit()
    return {"record_status": Candidate.ARCHIVED}


@router.post("/candidates/{candidate_id}/restore")
async def restore_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    """Undo an archive. Open to everyone, same as archive.

    Archiving is reversible by design — this is the other half of that
    promise. A merged row is refused for the same reason archive refuses one:
    unmerge must come first so `record_status` and
    `merged_into_candidate_id` never disagree about whether the row is live.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        if candidate.record_status == Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the candidate first")
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(record_status=Candidate.ACTIVE)
        )
        await session.commit()
    return {"record_status": Candidate.ACTIVE}


@router.post("/candidates/{candidate_id}/merge")
async def merge_candidate(
    request: Request, candidate_id: uuid.UUID, body: MergeRequest
) -> dict:
    _user_uuid, tenant_uuid = _require_session(request)
    if body.target_id == candidate_id:
        raise HTTPException(status_code=400, detail="A candidate cannot be merged into itself")

    async with tenant_session(tenant_uuid) as session:
        # Lock both rows before reading their statuses, or two opposing merges
        # (A→B and B→A at the same instant) each validate against a snapshot
        # taken before the other wrote, both succeed, and the two rows end up
        # pointing at each other: a cycle in which neither row is live, neither
        # appears in any list, and neither can be unmerged into a live record.
        #
        # Locked in a fixed order — lowest id first — so the two transactions
        # queue rather than deadlock. The statuses are only read *after* both
        # locks are held, because the pre-lock read is exactly what is stale.
        await _lock_pair(session, candidate_id, body.target_id)
        loser = await _load(session, candidate_id)
        target = await _load(session, body.target_id)
        if target.record_status == Candidate.MERGED:
            # Chains would need every reader to walk them. Refusing here is
            # what keeps the graph one hop deep.
            raise HTTPException(
                status_code=400, detail="Target is itself merged; merge into its target"
            )
        if loser.record_status == Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Candidate is already merged")

        # Someone may already point at the row we're about to merge. Refusing
        # would strand a recruiter who has three duplicates of one person and
        # no way to combine them; re-pointing those rows at the new target
        # keeps the graph one hop deep instead of forming a chain, and stays
        # inside this transaction so it commits or rolls back with the rest.
        await session.execute(
            update(Candidate)
            .where(
                Candidate.merged_into_candidate_id == candidate_id,
                Candidate.record_status == Candidate.MERGED,
            )
            .values(merged_into_candidate_id=body.target_id)
        )

        # Skills that the target already has would violate the per-candidate
        # unique key, so move only the ones it lacks and drop the rest — a
        # duplicate skill carries no information the target does not have.
        await session.execute(
            text(
                """
                DELETE FROM candidate_skills loser
                WHERE loser.candidate_id = :loser
                  AND EXISTS (
                      SELECT 1 FROM candidate_skills t
                      WHERE t.candidate_id = :target
                        AND t.skill_normalized = loser.skill_normalized
                  )
                """
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        await session.execute(
            text(
                "UPDATE candidate_skills SET candidate_id = :target WHERE candidate_id = :loser"
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        # Overrides move the same way, and for the same reason: they are a
        # record of what a person decided about this human being.
        await session.execute(
            text(
                """
                DELETE FROM candidate_field_overrides loser
                WHERE loser.candidate_id = :loser
                  AND EXISTS (
                      SELECT 1 FROM candidate_field_overrides t
                      WHERE t.candidate_id = :target AND t.field_name = loser.field_name
                  )
                """
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        await session.execute(
            text(
                "UPDATE candidate_field_overrides SET candidate_id = :target "
                "WHERE candidate_id = :loser"
            ),
            {"loser": candidate_id, "target": body.target_id},
        )
        # Status and target in one statement — a CHECK enforces that a merged
        # row names its target and a live row does not.
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(
                record_status=Candidate.MERGED, merged_into_candidate_id=body.target_id
            )
        )
        await session.commit()
    return {"record_status": Candidate.MERGED, "merged_into_candidate_id": str(body.target_id)}


@router.post("/candidates/{candidate_id}/unmerge")
async def unmerge_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    """Restore a merged candidate. Skills and overrides stay with the target.

    Deliberately partial: a moved row carries no record of which candidate it
    came from, so it cannot be given back. The identity keys return, which is
    what makes the person findable again.
    """
    _user_uuid, tenant_uuid = _require_session(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        if candidate.record_status != Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Candidate is not merged")

        # Unmerging returns this row's email and phone to the live indexes. If
        # somebody else took either in the meantime, restoring would violate a
        # unique index — so say who holds it rather than 500.
        clash = (
            await session.execute(
                text(
                    """
                    SELECT id, full_name FROM candidates
                    WHERE record_status <> 'merged' AND id <> :id
                      AND ((CAST(:email AS text) IS NOT NULL
                            AND lower(email) = lower(CAST(:email AS text)))
                        OR (CAST(:phone AS text) IS NOT NULL
                            AND phone_e164 = CAST(:phone AS text)))
                    LIMIT 1
                    """
                ),
                {"id": candidate_id, "email": candidate.email, "phone": candidate.phone_e164},
            )
        ).first()
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot unmerge: {clash.full_name} ({clash.id}) now holds "
                    "this candidate's email or phone."
                ),
            )

        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(record_status=Candidate.ACTIVE, merged_into_candidate_id=None)
        )
        await session.commit()
    return {"record_status": Candidate.ACTIVE}


@router.delete("/candidates/{candidate_id}", status_code=204)
async def delete_candidate(request: Request, candidate_id: uuid.UUID) -> Response:
    """Erase a person. Owner only, and irreversible.

    Skills and overrides cascade. Nothing else in phase 1 holds this person's
    personal data — the bulk import that will is built in phase 2, and its plan
    must extend this endpoint to scrub `candidate_import_rows`.
    """
    user_uuid, tenant_uuid = _require_session(request)
    await _require_owner(user_uuid, tenant_uuid)
    async with tenant_session(tenant_uuid) as session:
        await _load(session, candidate_id)
        await session.execute(delete(Candidate).where(Candidate.id == candidate_id))
        await session.commit()
    return Response(status_code=204)


@router.get("/candidates/{candidate_id}/export")
async def export_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    """Everything stored about one person, for a data-access request."""
    return await get_candidate(request, candidate_id)


def _identity_phone(raw: str | None) -> str | None:
    """The E.164 form, but only when this number may identify a person.

    `phone_e164` is a unique key, and `uq_candidates_tenant_phone` enforces it
    for every non-null value. A fixed line is shared by a whole company, so
    storing one here would let the second colleague who lists the office number
    be rejected as a duplicate of the first — while the matcher, which ignores
    fixed lines, reports no match at all. The recruiter would see "Already
    recorded" naming nobody.

    So a non-personal number lives in `phone_raw` only. It is still shown, it
    simply never identifies anyone, and the index and the matcher agree.
    """
    e164 = normalize_phone(raw)
    return e164 if is_matchable_phone(e164) else None


async def _require_owner(user_uuid: uuid.UUID, tenant_uuid: uuid.UUID) -> None:
    """The first role check in this codebase — see the spec.

    Archiving is what recruiters do daily and is open to everyone. Deleting is
    irreversible and covers personal data, so it is the owner's to do.
    """
    async with tenant_session(tenant_uuid) as session:
        role = (
            await session.execute(
                select(User.role).where(User.id == user_uuid)
            )
        ).scalar_one_or_none()
    if role != "owner":
        raise HTTPException(
            status_code=403, detail="Only the account owner can delete a candidate"
        )


async def _replace_skills(
    session, tenant_uuid: uuid.UUID, candidate_id: uuid.UUID, skills: list[str]
) -> None:
    """Skills are a set: the payload replaces them rather than appending.

    An append-only list has no way to remove a skill somebody typed by
    mistake, and a form that cannot unsay something is a form people distrust.
    """
    await session.execute(
        delete(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id)
    )
    seen: set[str] = set()
    for raw in skills:
        normalized = normalize_skill(raw)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        await session.execute(
            insert(CandidateSkill).values(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                candidate_id=candidate_id,
                skill=raw.strip(),
                skill_normalized=normalized,
            )
        )
