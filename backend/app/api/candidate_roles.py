"""The roles a candidate held.

A separate module from `candidates.py`, which is already close to the ceiling a
single file is allowed. The routes nest under the candidate because a role has
no meaning apart from one, and every one of them loads the candidate first, so
another agency's id is a 404 before a role is ever touched.
"""

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from app.api.auth import _require_session_with_role
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateRole
from app.models.extraction import ExtractionEvidence
from app.services.candidate_overrides import overridden_fields
from app.services.candidate_tenure import derive
from app.services.client_naming import normalize_company_name
from app.services.cv.schema import ROLE_FIELDS
from app.services.visibility import load_editable_candidate

router = APIRouter(tags=["candidate-roles"])

# The order `ROLE_FIELDS` puts a model's answer in, and the order in which a
# role's evidence is searched for the one line worth showing beside it. A CV
# role can carry a verified quote for several fields at once (title, company,
# both dates); the panel has room for one, and the title's line is the one
# that reads like "what the CV said about this job" rather than a fragment
# about a date.
_EVIDENCE_FIELD_PRIORITY = ROLE_FIELDS


class RoleBody(BaseModel):
    employer: str
    title: str
    started_on: date | None = None
    started_precision: str | None = None
    ended_on: date | None = None
    ended_precision: str | None = None
    employment_type: str | None = None
    location: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _ends_after_it_starts(self) -> "RoleBody":
        # `ck_candidate_roles_ends_after_start` says the same thing in the
        # database. Saying it here means the recruiter is told which end is
        # wrong, rather than meeting a 500 from a violated constraint.
        if self.started_on and self.ended_on and self.ended_on < self.started_on:
            raise ValueError("A role cannot end before it starts")
        if not self.employer.strip():
            raise ValueError("A role needs an employer")
        if not self.title.strip():
            raise ValueError("A role needs a title")
        for value in (self.started_precision, self.ended_precision):
            if value is not None and value not in CandidateRole.PRECISIONS:
                raise ValueError(
                    "A date's precision must be one of "
                    + ", ".join(CandidateRole.PRECISIONS)
                )
        return self


class RolePatchBody(BaseModel):
    """A patch, not a replacement.

    Every field defaults to unset rather than `None`, so `model_fields_set`
    can tell "the caller sent this key" apart from "the caller sent this key
    as null" apart from "the caller never mentioned this key". PATCH must
    only touch the keys actually sent — anything else silently wipes the
    fields a recruiter didn't mean to touch.
    """

    employer: str | None = None
    title: str | None = None
    started_on: date | None = None
    started_precision: str | None = None
    ended_on: date | None = None
    ended_precision: str | None = None
    employment_type: str | None = None
    location: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _sent_fields_are_sane(self) -> "RolePatchBody":
        # Cross-field date ordering can't be checked here in general — the
        # other end of the comparison may live on the stored row rather than
        # in this body. What we *can* reject up front is a blank string for a
        # field the caller did send, and a precision value outside the enum.
        if "employer" in self.model_fields_set and not (self.employer or "").strip():
            raise ValueError("A role needs an employer")
        if "title" in self.model_fields_set and not (self.title or "").strip():
            raise ValueError("A role needs a title")
        for field in ("started_precision", "ended_precision"):
            if field in self.model_fields_set:
                value = getattr(self, field)
                if value is not None and value not in CandidateRole.PRECISIONS:
                    raise ValueError(
                        "A date's precision must be one of "
                        + ", ".join(CandidateRole.PRECISIONS)
                    )
        return self


def _serialize(role: CandidateRole, evidence: str | None = None) -> dict:
    payload = {
        "id": str(role.id),
        "employer": role.employer,
        "employer_normalized": role.employer_normalized,
        "title": role.title,
        "title_normalized": role.title_normalized,
        "started_on": role.started_on.isoformat() if role.started_on else None,
        "started_precision": role.started_precision,
        "ended_on": role.ended_on.isoformat() if role.ended_on else None,
        "ended_precision": role.ended_precision,
        "employment_type": role.employment_type,
        "location": role.location,
        "description": role.description,
        "source": role.source,
        "status": role.status,
        # A role with no end date is the one the candidate is still in. Derived
        # rather than stored, so it cannot disagree with the dates beside it.
        "is_current": role.ended_on is None,
    }
    # Absent, not `None`-valued: a role a recruiter typed has no evidence row
    # at all, and the frontend tells the two apart by whether the key is
    # there — an empty disclosure for a human-entered role would be the
    # feature's honesty rule pointed the wrong way.
    if evidence:
        payload["evidence"] = evidence
    return payload


def _ordered(candidate_id: uuid.UUID):
    """Current first, then newest, with undated roles last.

    A recruiter reads a career from the top, and the row they want is almost
    always the one the person is in now. A role with no dates at all still
    belongs on the page — it is simply the least useful thing there.
    """
    return (
        select(CandidateRole)
        .where(CandidateRole.candidate_id == candidate_id)
        .order_by(
            CandidateRole.ended_on.is_(None).desc(),
            CandidateRole.started_on.desc().nullslast(),
            CandidateRole.created_at.desc(),
        )
    )


async def roles_for(session, candidate_id: uuid.UUID) -> list[CandidateRole]:
    return list((await session.execute(_ordered(candidate_id))).scalars())


async def evidence_for(session, role_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """One quote per role, keyed by role id — a single query for the whole page.

    Loaded once for the full role list rather than per role, so the detail
    panel stays at the query count it already had. Only `evidence_valid` rows
    are candidates: an unverified span is the exact thing §15 says not to
    assert, so it must never reach a client, verified or not.

    A role can have several verified fields (title, company, both dates); the
    panel shows one line, so `_EVIDENCE_FIELD_PRIORITY` picks whichever field
    ranks first rather than an arbitrary row order the database happens to
    return.
    """
    if not role_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ExtractionEvidence.candidate_role_id,
                ExtractionEvidence.field_name,
                ExtractionEvidence.evidence_text,
            ).where(
                ExtractionEvidence.candidate_role_id.in_(role_ids),
                ExtractionEvidence.evidence_valid.is_(True),
            )
        )
    ).all()

    by_role: dict[uuid.UUID, dict[str, str]] = {}
    for role_id, field_name, evidence_text in rows:
        if not evidence_text:
            continue
        by_role.setdefault(role_id, {})[field_name] = evidence_text

    result: dict[uuid.UUID, str] = {}
    for role_id, fields in by_role.items():
        for field_name in _EVIDENCE_FIELD_PRIORITY:
            if field_name in fields:
                result[role_id] = fields[field_name]
                break
    return result


async def apply_derived(session, candidate: Candidate) -> None:
    """Write the derived profile back onto the candidate row.

    Called inside the same transaction as the mutation that changed the roles,
    so a failed recompute takes the role change with it. A role saved beside a
    candidate row that still disagrees with it is exactly the drift this design
    exists to prevent.
    """
    roles = await roles_for(session, candidate.id)
    # Derivation writes the SHARED candidate row, so the reader here is the
    # owner, not whoever happens to be editing roles: a colleague's private
    # reading must not stop the base row being re-derived, and the owner's
    # must.
    overridden = await overridden_fields(session, candidate.id, candidate.owner_id)
    # `derive` itself drops rejected roles, so a candidate whose every role is
    # rejected has nothing for it to work from. Checking the raw list here
    # would take the "has roles" branch anyway and derive from an empty set —
    # match derive's own idea of "has roles" instead of the row count.
    if not [role for role in roles if role.status != CandidateRole.REJECTED]:
        # Null the derived columns rather than leaving them stale. A value like
        # `current_employer` was derived from the very role the recruiter just
        # removed — if they deleted it because it was wrong, keeping the
        # derived value preserves precisely the wrong data, and with no
        # CandidateFieldOverride recorded it is indistinguishable from the
        # truth. §15 forbids asserting a fact no source states; a candidate
        # with no source for these fields must not keep displaying one.
        # Overridden fields are a person's own assertion, not derivation's, so
        # they survive here exactly as they survive normal re-derivation.
        if "current_title" not in overridden:
            candidate.current_title = None
        if "current_employer" not in overridden:
            candidate.current_employer = None
        if "years_experience" not in overridden:
            candidate.years_experience = None
        return

    profile = derive(roles, today=date.today())

    if "current_title" not in overridden and profile.current_title:
        candidate.current_title = profile.current_title
    if "current_employer" not in overridden and profile.current_employer:
        candidate.current_employer = profile.current_employer
    # Not `and profile.years_experience is not None`: unlike `current_title`
    # and `current_employer`, which `derive` always fills in from `latest`
    # whenever this branch is reached (there is always a live role to name),
    # `years_experience` legitimately comes back `None` here — every
    # surviving role is undated, so there is nothing to sum a span from. A
    # guard that skips the write on `None` would leave whatever tenure was
    # cached from a role that no longer supports it, which is exactly the
    # stale-assertion §15 forbids. So the write always happens; only the
    # override check gates it.
    if "years_experience" not in overridden:
        candidate.years_experience = profile.years_experience


def _values(body: RoleBody) -> dict:
    # `employer_normalized` goes through the same function a client's name
    # does: an employer on a CV and a client in the agency's list are the same
    # kind of string, and matching them later only works if they were
    # normalised identically.
    #
    # Titles get the plainest normalisation there is. `normalize_skill` exists
    # nearby but is tuned for skill tokens, and borrowing it would quietly
    # apply rules nobody chose for job titles.
    return {
        "employer": body.employer.strip(),
        "employer_normalized": normalize_company_name(body.employer),
        "title": body.title.strip(),
        "title_normalized": body.title.strip().lower(),
        "started_on": body.started_on,
        "started_precision": body.started_precision,
        "ended_on": body.ended_on,
        "ended_precision": body.ended_precision,
        "employment_type": body.employment_type,
        "location": body.location,
        "description": body.description,
    }


async def _load_role(session, candidate_id: uuid.UUID, role_id: uuid.UUID) -> CandidateRole:
    """Scoped by the candidate as well as the role's own id.

    A role id alone would let one candidate's history be edited through
    another's URL — inside the tenant, but still a record nobody asked to
    change.
    """
    role = (
        await session.execute(
            select(CandidateRole).where(
                CandidateRole.id == role_id,
                CandidateRole.candidate_id == candidate_id,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


@router.post("/candidates/{candidate_id}/roles", status_code=201)
async def create_role(request: Request, candidate_id: uuid.UUID, body: RoleBody) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_editable_candidate(session, candidate_id, user_uuid, role)
        role = CandidateRole(
            id=uuid.uuid4(),
            tenant_id=tenant_uuid,
            candidate_id=candidate.id,
            source=CandidateRole.HUMAN,
            status=CandidateRole.CONFIRMED,
            created_by=user_uuid,
            updated_by=user_uuid,
            **_values(body),
        )
        session.add(role)
        await session.flush()
        await apply_derived(session, candidate)
        await session.commit()
        evidence = (await evidence_for(session, [role.id])).get(role.id)
        return _serialize(role, evidence)


@router.patch("/candidates/{candidate_id}/roles/{role_id}")
async def update_role(
    request: Request, candidate_id: uuid.UUID, role_id: uuid.UUID, body: RolePatchBody
) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_editable_candidate(session, candidate_id, user_uuid, role)
        role = await _load_role(session, candidate_id, role_id)

        sent = body.model_fields_set
        if "employer" in sent:
            role.employer = body.employer.strip()
            role.employer_normalized = normalize_company_name(body.employer)
        if "title" in sent:
            role.title = body.title.strip()
            role.title_normalized = body.title.strip().lower()
        for field in (
            "started_on",
            "started_precision",
            "ended_on",
            "ended_precision",
            "employment_type",
            "location",
            "description",
        ):
            if field in sent:
                setattr(role, field, getattr(body, field))

        # The comparison the model validator runs on a full body still has to
        # hold after a partial one — but one side of it may be a value the
        # caller never sent, sitting only on the row we just merged onto. So
        # this check has to happen post-merge, against the resulting role,
        # not against the patch body in isolation.
        if role.started_on and role.ended_on and role.ended_on < role.started_on:
            # Same sentence and the same `detail[0]["msg"]` shape the
            # `RoleBody` validator produces on create, so a client doesn't
            # need a second error format to handle the patch path.
            raise HTTPException(
                status_code=422,
                detail=[{"msg": "Value error, A role cannot end before it starts"}],
            )

        role.updated_by = user_uuid
        await session.flush()
        await apply_derived(session, candidate)
        await session.commit()
        evidence = (await evidence_for(session, [role.id])).get(role.id)
        return _serialize(role, evidence)


async def _set_status(
    request: Request, candidate_id: uuid.UUID, role_id: uuid.UUID, status: str
) -> dict:
    """The shared body of confirm and reject.

    Both re-derive, and both do it in the same transaction as the status
    change. Confirming a role can make it the candidate's current one;
    rejecting the role that *was* current takes that employer away again —
    `derive` drops rejected roles, so the recompute is what keeps the
    candidate row from asserting a fact the recruiter just denied (§15).

    Idempotent on purpose: confirming an already-confirmed role is a
    double-click, not an error.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_editable_candidate(session, candidate_id, user_uuid, role)
        role = await _load_role(session, candidate_id, role_id)
        role.status = status
        role.updated_by = user_uuid
        await session.flush()
        await apply_derived(session, candidate)
        await session.commit()
        evidence = (await evidence_for(session, [role.id])).get(role.id)
        return _serialize(role, evidence)


@router.post("/candidates/{candidate_id}/roles/{role_id}/confirm")
async def confirm_role(request: Request, candidate_id: uuid.UUID, role_id: uuid.UUID) -> dict:
    """A person vouches for what the parse read off the CV."""
    return await _set_status(request, candidate_id, role_id, CandidateRole.CONFIRMED)


@router.post("/candidates/{candidate_id}/roles/{role_id}/reject")
async def reject_role(request: Request, candidate_id: uuid.UUID, role_id: uuid.UUID) -> dict:
    """A person says the parse got this wrong.

    Kept rather than deleted: the row is evidence of what the model claimed,
    and a re-parse of the same CV should not resurrect a role somebody has
    already thrown out.
    """
    return await _set_status(request, candidate_id, role_id, CandidateRole.REJECTED)


@router.delete("/candidates/{candidate_id}/roles/{role_id}", status_code=204)
async def delete_role(request: Request, candidate_id: uuid.UUID, role_id: uuid.UUID) -> Response:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_editable_candidate(session, candidate_id, user_uuid, role)
        role = await _load_role(session, candidate_id, role_id)
        await session.delete(role)
        await session.flush()
        await apply_derived(session, candidate)
        await session.commit()
    return Response(status_code=204)
