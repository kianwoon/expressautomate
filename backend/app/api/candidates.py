"""The agency's candidate list.

Nothing here is AI-derived. Every value was typed by a person or came from a
spreadsheet a person uploaded, so there is no confidence, no evidence, and no
review queue — only records and the people who edited them.
"""

import re
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import case, delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.api.auth import _require_session, _require_session_with_role
from app.api.integrity import is_duplicate as _is_duplicate
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import (
    Candidate,
    CandidateActivity,
    CandidateFieldOverride,
    CandidateLanguage,
    CandidateSkill,
)
from app.models.tenant import User
from app.services.candidate_matching import find_candidate, held_by_colleague, patch_collision
from app.services.candidate_naming import (
    is_matchable_phone,
    normalize_email,
    normalize_language,
    normalize_phone,
    normalize_skill,
)
from app.services.candidate_overrides import JUDGEMENT_FIELDS, overridden_fields
from app.services.candidate_tenure import derive
from app.services.name_index import initial_of as _initial_of
from app.services.name_index import sorted_initials as _sorted_initials
from app.services.sourcing import eligibility
from app.services.user_naming import actor_name
from app.services.visibility import (
    can_edit_candidate,
    candidate_scope,
    load_editable_candidate,
    load_visible_candidate,
    load_visible_opportunity,
    visible_candidates,
)

log = get_logger(__name__)
router = APIRouter(tags=["candidates"])

StageFilter = Literal["new", "contacted", "submitted", "placed", "rejected"]
RecordStatusFilter = Literal["active", "archived", "merged"]

# The five list columns a recruiter may sort by, mirroring `opportunities.py`'s
# sort for the job-orders table. The default (`updated`) preserves the previous
# fixed order — `updated_at` desc — so an unsorted list is byte-for-byte what it
# was before the parameter existed.
CandidateSortKey = Literal["name", "title", "employer", "stage", "updated"]

# The pipeline_stage column is text, but its natural order is the hiring
# funnel (`new` → `placed`), not alphabetical. A recruiter clicking "Stage"
# means "show me how far down the pipeline people are", so the order is pinned
# to that vocabulary by rank. `rejected` sits last in both directions: it is
# the end of the road, not the start.
_STAGE_RANK = {
    "new": 0, "contacted": 1, "submitted": 2, "placed": 3, "rejected": 4,
}


def _stage_rank():
    # `else_=0` rather than NULL: a row whose stage is somehow off-vocabulary
    # (impossible once `ck_candidates_pipeline_stage` holds, kept for `case`'s
    # required else) sorts at the top of an ascending list, exactly as
    # `opportunities._quality_rank` does for the same reason.
    return case(
        *[(Candidate.pipeline_stage == stage, rank) for stage, rank in _STAGE_RANK.items()],
        else_=0,
    )


# Column (or expression) each sort key orders by, text columns lowered so the
# order is case-insensitive — "acme" and "Acme" are the same employer and an
# uppercase-first ordering would file them pages apart.
_CANDIDATE_SORT_COLUMN = {
    "name": func.lower(Candidate.full_name),
    "title": func.lower(Candidate.current_title),
    "employer": func.lower(Candidate.current_employer),
    "stage": _stage_rank,
    "updated": Candidate.updated_at,
}


def _candidate_order_by(sort: str, descending: bool) -> tuple:
    """The ORDER BY clause for a sort key, nulls sinking in both directions.

    Mirrors `opportunities._order_by`: SQL does not sink NULLs for you, both
    directions are spelled out, and `id.desc()` breaks ties so paging stays
    stable regardless of which column is being sorted.
    """
    column = _CANDIDATE_SORT_COLUMN[sort]
    if callable(column):
        column = column()
    ordered = column.desc().nulls_last() if descending else column.asc().nulls_last()
    return (ordered, Candidate.id.desc())

# The regulatory vocabularies, named off the model so the request schema and
# the database CHECK cannot drift apart. These are accepted on write and shown
# on read; deliberately NOT offered as query parameters anywhere — see
# `list_candidates`, and `app/services/sourcing/redact.py` for why.
SexIn = Literal[Candidate.FEMALE, Candidate.MALE]
RaceIn = Literal["chinese", "malay", "indian", Candidate.OTHERS]
FluencyIn = Literal[
    CandidateLanguage.NATIVE,
    CandidateLanguage.FLUENT,
    CandidateLanguage.CONVERSATIONAL,
    CandidateLanguage.BASIC,
]

# A single letter or `#`; anything else is a 422 from the framework rather than
# a hand-rolled check, so the contract lives in the OpenAPI schema too. Shared
# with `clients.py` via the same `Query(...)` shape, but kept here as a local
# singleton because B008 flags a `Query(...)` in a signature default and a
# module-level instance is the workaround.
InitialFilter = Query(default=None, pattern=r"^([A-Za-z]|#)$")

# Module-level singleton, the same reason `InitialFilter` is one: a `Query(...)`
# call sitting in the signature default is what B008 exists to catch, since a
# mutable default is built once at import and shared across every request.
EligibleForFilter = Query(default=None)


# No `_LEGAL_SOURCES` whitelist here, unlike clients.py: a candidate only has
# `active | archived | merged`, and archive/restore are exact inverses of each
# other, so there is no illegal-source combination to guard against.


# The same coalesce `held_by_colleague` uses, and for the same reason:
# `users` has no `full_name`, so prefer what the person chose to be called,
# then what the directory says, then the address they signed in with — never
# a bare UUID, which tells the recruiter nothing and looks like a bug.
_OWNER_NAME = func.coalesce(User.preferred_name, User.display_name, User.email)


def _with_owner_name(query):
    """LEFT OUTER, once, onto `users.id`.

    Outer, not inner: an unclaimed candidate has no owner row and an inner
    join would silently drop the whole unclaimed queue. Once, not per row:
    the owner's name is fetched by the same statement that fetches the page,
    so a fifty-row list still costs one query rather than fifty-one.

    Applied only to the statement that fetches rows — never to the COUNT or
    the initials aggregate, which keep reading the un-joined `base`. It
    cannot change what those would have said in any case: `users.id` is a
    primary key, so the join is one-to-one and cannot fan a row out.
    """
    return query.add_columns(_OWNER_NAME.label("owner_name")).outerjoin(
        User, User.id == Candidate.owner_id
    )


def _serialize(
    candidate: Candidate, owner_name: str | None, user_id: uuid.UUID, role: str
) -> dict:
    return {
        "id": str(candidate.id),
        # Who holds this person, and whether THIS reader may change it. Both
        # are required together: the share recipient's edit control is
        # disabled rather than hidden, and a disabled control has to say who
        # to go to. `can_edit` delegates to `can_edit_candidate` rather than
        # restating "the owner, or the agency owner" — a second copy of that
        # rule is a second place for it to drift.
        "owner": (
            {"id": str(candidate.owner_id), "name": owner_name}
            if candidate.owner_id
            else None
        ),
        "can_edit": can_edit_candidate(candidate, user_id, role),
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
        "last_drawn_salary": (
            float(candidate.last_drawn_salary)
            if candidate.last_drawn_salary is not None
            else None
        ),
        "last_drawn_currency": candidate.last_drawn_currency,
        "last_drawn_period": candidate.last_drawn_period,
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
        # Both are read by `candidate-avatar.tsx` and neither is decoration.
        # `avatar_key` is how the panel knows there is a photo to ask for at
        # all — absent it, the component cannot tell "no photo" from "photo it
        # has not fetched yet", and skipping the request on a falsy value
        # silently skips it for everyone. `avatar_updated_at` is the version
        # its URL cache keys on, so without it a replaced photo keeps the key
        # the old one was cached under. `clients.py` has always sent the
        # equivalent pair; this is the same contract, not a new one.
        "avatar_key": candidate.avatar_key,
        "avatar_updated_at": (
            candidate.avatar_updated_at.isoformat()
            if candidate.avatar_updated_at
            else None
        ),
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
    }


# The one 409 shape `?eligible_for=` uses when the opportunity has no
# `placement_type`. A flat body, deliberately not `HTTPException(detail={...})`
# — that nests everything under `detail`, and the frontend needs `reason` as a
# sibling key it can branch on without unwrapping. Mirrors `_rate_limited` in
# `candidate_whatsapp.py`, the established shape for this codebase's non-
# `HTTPException` error responses.
def _placement_type_not_set() -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": (
                "This job order has no placement type set, so there is no "
                "regulatory rule to filter candidates against."
            ),
            "reason": "placement_type_not_set",
        },
    )


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
    initial: str | None = InitialFilter,
    scope: Literal["mine", "queue", "shared_with_me", "all"] = "all",
    # The column to sort by, plus its direction. Defaults to `updated` desc —
    # the fixed order the list had before sorting landed — so a caller who sends
    # neither sees the same rows in the same order it always did. `initial`
    # overrides both: an A–Z letter pins the list to that initial and sorts
    # within it by name ascending, exactly as it always has, so the sort
    # parameter is ignored when a letter is active (see `order` below).
    sort: CandidateSortKey = "updated",
    descending: bool = True,
    # A recruiter narrowing a shortlist to who a placement's regulatory rules
    # (MOM's, not the job's own occupational sex requirement — see
    # `eligibility.has_regulatory_not_met`) do not definitely disqualify.
    # Absent entirely means no eligibility filtering, exactly as before this
    # parameter existed — `race`, `sex`, `nationality` etc. still have no
    # query parameter of their own (see the comment above `_user_uuid,
    # tenant_uuid = ...` below) and this is not one either: it filters on what
    # a job order's stated rule computes, never on a raw demographic column.
    eligible_for: uuid.UUID | None = EligibleForFilter,
) -> dict:
    # There is deliberately no `sex`, `race`, `nationality`, `date_of_birth`,
    # `education_years` or `language` parameter here, and adding one is not a
    # small change. Those columns exist because a MOM form asks for them; a
    # filter on them is the platform helping somebody shortlist on a protected
    # characteristic, which is the exact thing `app/services/sourcing/
    # redact.py` exists to prevent. An unrecognised query parameter is ignored
    # by FastAPI, so a caller who sends `?race=chinese` gets the unfiltered
    # list rather than an error — no filtering, and no hint that filtering is
    # around the corner. `tests/test_candidate_demographics_api.py` asserts it.
    #
    # Eligibility matching (a MDW Work Permit genuinely requires female, 23 to
    # under 50, eight years of education, an approved source country) is a
    # job-order concern and belongs behind a job order that states the rule,
    # not behind a free-text filter on the whole candidate list.
    #
    # The role is read here, unlike every other route in this module, because
    # `?eligible_for=` reads a job order by id and who may see a job order is
    # a per-recruiter rule (`app/services/visibility.py`), not a per-agency
    # one. Every other branch reads only candidates, which RLS alone scopes.
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    ceiling = settings.CANDIDATES_PAGE_LIMIT
    page_limit = ceiling if limit is None else min(limit, ceiling)

    async with tenant_session(tenant_uuid) as session:
        # Counted over the whole tenant, before any filter or window, so a
        # chip does not change meaning as the recruiter pages.
        counts = {"all": 0}
        for stage, n in await session.execute(
            select(Candidate.pipeline_stage, func.count())
            .where(Candidate.record_status != Candidate.MERGED)
            # The chips count what this recruiter may see. Counting the whole
            # tenant would leak the size of a colleague's book.
            .where(visible_candidates(user_uuid, role), candidate_scope(scope, user_uuid))
            # Placeholder "Uploaded CV" rows are hidden from the list, so the
            # count must exclude them too — otherwise "All 21" sits above a
            # table of 18 (the list hides placeholders, the count did not,
            # and a CV upload in the 2-5s ingest window over-reports).
            .where(Candidate.full_name != Candidate.PLACEHOLDER_NAME)
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
        base = base.where(visible_candidates(user_uuid, role), candidate_scope(scope, user_uuid))
        # Hide placeholder rows — a CV-upload creates one synchronously before
        # the ingest worker reads identity and either renames or deletes it.
        # Showing "Uploaded CV" for those 2-5 seconds reads as a ghost
        # candidate. Excluded everywhere except an explicit search for it.
        if not (q and Candidate.PLACEHOLDER_NAME.lower() in (q.strip().lower())):
            base = base.where(Candidate.full_name != Candidate.PLACEHOLDER_NAME)
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

        # Deliberately computed from `base` *before* `initial` narrows it: the
        # bar answers "which letters could I click next", and applying the
        # letter already clicked would leave a bar of exactly one letter with
        # no way back to the rest. One aggregate over the whole filtered set,
        # not twenty-seven counting queries.
        unfiltered = base.subquery()
        initials = _sorted_initials(
            list(
                (
                    await session.execute(
                        select(_initial_of(unfiltered.c.full_name))
                        .select_from(unfiltered)
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
        )

        if initial is not None:
            base = base.where(_initial_of(Candidate.full_name) == initial.upper())

        # The order follows what the reader is doing. Browsing the whole list
        # is a "what changed lately" question, so recency wins. Clicking a
        # letter is a "find this person" question, and recency inside a letter
        # reads as no order at all — you cannot scan for a surname in it.
        # `id` last, on both branches, because neither key above is unique. A
        # bulk import gives thousands of rows the same `updated_at`, and two
        # people genuinely share a name — and where the sort key ties, Postgres
        # is free to return them in a different order each time it is asked.
        # Paging then shows somebody twice and somebody else not at all, which
        # reads as the list losing people rather than as an unstable sort.
        #
        # It matters more since `?eligible_for=` arrived: the same order decides
        # where the bounded scan is cut, so an unstable tail would change *which*
        # candidates are assessed between one request and the next.
        #
        # The A–Z index (`initial`) overrides the sort: a recruiter who picks a
        # letter is standing in that slice, sorted by name — re-sorting it would
        # detach the list from the index that produced it. Without a letter, the
        # recruiter's chosen column and direction apply, defaulting to the
        # `updated_at` desc the list always had.
        order = (
            (func.lower(Candidate.full_name).asc(), Candidate.id.asc())
            if initial is not None
            else _candidate_order_by(sort, descending)
        )

        excluded_ineligible: int | None = None
        scan_truncated: bool | None = None
        scanned: int | None = None
        if eligible_for is not None:
            # Two boundaries, not one, and RLS only draws the first.
            #
            # §18: another agency's opportunity id must read as 404, not 403 —
            # `_load` in this module gives that answer for a candidate for the
            # same reason, and `tenant_session` scopes every SELECT under RLS
            # exactly as it does everywhere else, so a foreign id matches no
            # row here rather than leaking whether it exists.
            #
            # The second boundary runs inside one agency, and RLS says nothing
            # about it: a job order belongs to the recruiter it is assigned to.
            # Reading it under the tenant policy alone would let any colleague
            # holding the id — from a share since revoked, say — learn its
            # `placement_type` and pull the shortlist it filters.
            # `load_visible_opportunity` applies that rule and raises the same
            # 404, never a 403, for exactly the same reason as above.
            opportunity = await load_visible_opportunity(
                session, eligible_for, user_uuid, role
            )
            if opportunity.placement_type is None:
                return _placement_type_not_set()

            # No N+1: every fact `eligibility.evaluate` needs (sex,
            # date_of_birth, education_years, nationality) is already a column
            # on `Candidate`, so the rows this query fetches are the only
            # database round trip eligibility filtering costs — evaluation
            # itself is pure Python over data already in memory, exactly like
            # `get_eligibility` in `opportunities.py`.
            #
            # Fetched unpaginated (up to the scan ceiling) and ordered the
            # same way the plain list would be: `not_met` is only known after
            # evaluating every matching row, so the page a `LIMIT`/`OFFSET`
            # would carve out cannot be decided in SQL without
            # re-implementing the rules there — which the eligibility
            # module's docstring forbids.
            #
            # `+ 1` over the ceiling is how truncation is detected without a
            # second COUNT query: fetching one more row than the ceiling
            # allows tells us whether there *is* a next row, at the cost of
            # discarding it if so.
            scan_limit = settings.CANDIDATES_ELIGIBILITY_SCAN_LIMIT
            scan_rows = (
                await session.execute(
                    _with_owner_name(base).order_by(*order).limit(scan_limit + 1)
                )
            ).all()
            scan_truncated = len(scan_rows) > scan_limit
            all_rows = scan_rows[:scan_limit]
            scanned = len(all_rows)

            kept: list[tuple[Candidate, str | None]] = []
            for candidate, _owner_name in all_rows:
                facts = eligibility.CandidateFacts(
                    sex=candidate.sex,
                    date_of_birth=candidate.date_of_birth,
                    education_years=candidate.education_years,
                    nationality=candidate.nationality,
                )
                findings = eligibility.evaluate(
                    opportunity.placement_type,
                    facts,
                    as_of=date.today(),
                    min_age_years=settings.MDW_MIN_AGE_YEARS,
                    max_age_years_exclusive=settings.MDW_MAX_AGE_YEARS_EXCLUSIVE,
                    min_education_years=settings.MDW_MIN_EDUCATION_YEARS,
                    approved_source_countries=settings.MDW_APPROVED_SOURCE_COUNTRIES,
                    sex_requirement=opportunity.sex_requirement,
                    sex_requirement_reason=opportunity.sex_requirement_reason,
                )
                if not eligibility.has_regulatory_not_met(findings):
                    kept.append((candidate, _owner_name))
            excluded_ineligible = len(all_rows) - len(kept)
            total = len(kept)
            rows = kept[offset : offset + page_limit]
        else:
            total = (
                await session.execute(select(func.count()).select_from(base.subquery()))
            ).scalar_one()
            rows = (
                await session.execute(
                    _with_owner_name(base)
                    .order_by(*order)
                    .limit(page_limit)
                    .offset(offset)
                )
            ).all()

    payload = {
        "items": [_serialize(c, owner_name, user_uuid, role) for c, owner_name in rows],
        "total": total,
        "limit": page_limit,
        "offset": offset,
        # Both describe the unfiltered population, so under `?eligible_for=`
        # they describe a different set of people than `items` does — a chip
        # reading "All 4,200" above thirty rows, and letters that lead to empty
        # pages. Recomputing them over the eligible set is not the fix either:
        # the eligibility pass only ever sees the scan window, so a stage count
        # taken from it would be a confident number for a question nobody asked
        # ("how many eligible people are in Screening, among the first five
        # thousand by recency").
        #
        # Not that an honest number is never available — when the scan did not
        # truncate it saw the whole matching set, which for most agencies here
        # is every time. Suppressing it even then is a choice, and the reason
        # is that the alternative is worse to read: chips that carry figures on
        # a small agency and fall silent on a large one teach nobody what the
        # number means, and the recruiter who most needs the caveat is the one
        # who would stop seeing it. So the caller is told to stop drawing the
        # chips and the letter bar, rather than to draw them sometimes.
        "counts": None if eligible_for is not None else counts,
        "initials": None if eligible_for is not None else initials,
    }
    if excluded_ineligible is not None:
        payload["excluded_ineligible"] = excluded_ineligible
        # Present only alongside `excluded_ineligible` — the unfiltered path
        # never truncates (it pages in SQL) and must not gain a field that
        # would suggest it might. `scanned` is how many candidates the
        # eligibility rules were actually evaluated against; `scan_truncated`
        # is whether that number is the tenant's whole matching set or just
        # the ceiling. A caller must not read a short `total` as a complete
        # answer without checking this.
        payload["scanned"] = scanned
        payload["scan_truncated"] = scan_truncated
    return payload


@router.get("/candidates/{candidate_id}")
async def get_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_visible_candidate(session, candidate_id, user_uuid, role)
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
        languages = (
            (
                await session.execute(
                    select(CandidateLanguage)
                    .where(CandidateLanguage.candidate_id == candidate_id)
                    .order_by(CandidateLanguage.language_normalized)
                )
            )
            .scalars()
            .all()
        )
        # The signed-in reader, not the candidate's owner: this route renders
        # what THIS recruiter asserted plus what the agency asserted.
        overrides = await overridden_fields(session, candidate_id, user_uuid)
        # Imported here rather than at module scope: `candidate_roles` imports
        # `_load` from this module, and a top-level import each way would not
        # resolve. The detail panel is the only reader, so the cost is one
        # lookup on a route that already does three queries.
        from app.api.candidate_documents import documents_for
        from app.api.candidate_documents import serialize as _serialize_document
        from app.api.candidate_roles import _serialize as _serialize_role
        from app.api.candidate_roles import evidence_for, roles_for

        roles = await roles_for(session, candidate_id)
        # One query for every role's evidence rather than one per role — the
        # roles are already loaded, so a second round trip per row would be
        # the N+1 the rest of this endpoint is careful to avoid.
        evidence = await evidence_for(session, [r.id for r in roles])
        documents = await documents_for(session, candidate_id)
        # One row, so there is no N+1 to avoid here — the list's join exists
        # because it serialises a page at a time; this route serialises one.
        owner_name = (
            (
                await session.execute(
                    select(_OWNER_NAME).where(User.id == candidate.owner_id)
                )
            ).scalar_one_or_none()
            if candidate.owner_id
            else None
        )

    payload = _serialize(candidate, owner_name, user_uuid, role)
    # Only on the single-record read, deliberately not on a list row.
    #
    # These exist so a recruiter can fill a MOM form for the person they have
    # opened. Putting them on every row of the table would print race beside
    # fifty names at once, and a screen like that invites shortlisting by eye —
    # which is the thing this platform refuses to do in code (`redact.py`), and
    # refusing it in code while laying it out on a page would be a distinction
    # without a difference.
    payload |= {
        "sex": candidate.sex,
        "race": candidate.race,
        "race_detail": candidate.race_detail,
        "nationality": candidate.nationality,
        # The date as stored. No age is derived here or anywhere — eligibility
        # is judged at application, so a computed age would be wrong within the
        # year and wrong in a direction nobody would notice.
        "date_of_birth": (
            candidate.date_of_birth.isoformat() if candidate.date_of_birth else None
        ),
        "education_years": candidate.education_years,
    }
    payload["skills"] = [s.skill for s in skills]
    # Objects, not bare strings as skills are: a language without its fluency
    # is half the fact, and flattening it would lose the half that decides a
    # placement.
    payload["languages"] = [
        {"language": row.language, "fluency": row.fluency} for row in languages
    ]
    # So the UI can say why an import did not change a field, rather than
    # leaving the recruiter to conclude the import is broken.
    payload["overridden_fields"] = sorted(overrides)
    # Only the single-record GET carries the career. A table of fifty
    # candidates does not need everybody's.
    payload["roles"] = [_serialize_role(r, evidence.get(r.id)) for r in roles]
    # Beside the roles, and for the same reason: a recruiter looking at an
    # unconfirmed role needs to see the upload it came from, and whether that
    # upload is still being read.
    payload["documents"] = [_serialize_document(d) for d in documents]

    # Derived fresh rather than read from the column: a role with no end date
    # is still accruing, so the cached value is stale the month after it was
    # written. Not persisted here — a GET that writes is a GET that deadlocks
    # under load, and the column stays the cache the list and search read.
    profile = derive(roles, today=date.today())
    # Not gated on `is not None`: a candidate whose surviving roles are all
    # undated derives `years_experience=None` even though the roles list is
    # non-empty, and skipping the write here would serve the stale cached
    # column instead — the same §15 gap `apply_derived` guards against on the
    # write side. The override check is what must gate this, not the value.
    if "years_experience" not in set(overrides):
        payload["years_experience"] = profile.years_experience
    # What lets the panel say "Most recently" instead of "Current" for a
    # candidate who is between jobs.
    payload["is_current"] = profile.is_current
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

# Two uppercase letters, the ISO 3166-1 alpha-2 shape. The database CHECK says
# the same; this is here so the caller gets a 422 naming the field.
_ISO_ALPHA2 = re.compile(r"[A-Z]{2}")


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

    @field_validator("nationality", check_fields=False)
    @classmethod
    def _nationality_is_iso_alpha2(cls, value: str | None) -> str | None:
        """Uppercased here so `sg` and `SG` are the same country.

        `ck_candidates_nationality_iso_alpha2` says the same thing in the
        database; saying it here means the caller is told which field is wrong
        instead of getting a 500 out of a CHECK.
        """
        if value is None:
            return None
        upper = value.strip().upper()
        if not _ISO_ALPHA2.fullmatch(upper):
            raise ValueError("nationality must be an ISO 3166-1 alpha-2 code, e.g. 'PH'")
        return upper

    @field_validator("education_years", check_fields=False)
    @classmethod
    def _education_years_are_plausible(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not (
            Candidate.EDUCATION_YEARS_MIN <= value <= Candidate.EDUCATION_YEARS_MAX
        ):
            raise ValueError(
                "education_years must be between "
                f"{Candidate.EDUCATION_YEARS_MIN} and {Candidate.EDUCATION_YEARS_MAX}"
            )
        return value


class LanguageIn(BaseModel):
    """One language a candidate speaks. `fluency` is optional on purpose.

    A recruiter who knows somebody speaks Tagalog but has not assessed how
    well records the language and leaves the level unset, rather than picking
    one to satisfy a form (§15).
    """

    language: str
    fluency: FluencyIn | None = None


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
    last_drawn_salary: float | None = None
    last_drawn_currency: str | None = None
    last_drawn_period: str | None = None
    available_from: date | None = None
    notice_period_raw: str | None = None
    employment_type: str | None = None
    # Regulatory facts, all optional. Omitting one leaves it NULL — not
    # recorded — and nothing infers a value for it.
    sex: SexIn | None = None
    race: RaceIn | None = None
    race_detail: str | None = None
    nationality: str | None = None
    date_of_birth: date | None = None
    education_years: int | None = None
    notes: str | None = None
    pipeline_stage: StageFilter | None = None
    skills: list[str] | None = None
    languages: list[LanguageIn] | None = None


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
    last_drawn_salary: float | None = None
    last_drawn_currency: str | None = None
    last_drawn_period: str | None = None
    available_from: date | None = None
    notice_period_raw: str | None = None
    employment_type: str | None = None
    # Regulatory facts, all optional. Omitting one leaves it NULL — not
    # recorded — and nothing infers a value for it.
    sex: SexIn | None = None
    race: RaceIn | None = None
    race_detail: str | None = None
    nationality: str | None = None
    date_of_birth: date | None = None
    education_years: int | None = None
    notes: str | None = None
    pipeline_stage: StageFilter | None = None
    skills: list[str] | None = None
    languages: list[LanguageIn] | None = None


# Fields a human edit protects from a later import. `skills` and `languages`
# are excluded: each is a set, not a value, and merging an imported member into
# a curated list loses nothing.
_OVERRIDABLE = (
    "full_name", "email", "phone_raw", "current_title", "current_employer",
    "location", "years_experience", "expected_salary", "salary_currency",
    "salary_period",
    "last_drawn_salary", "last_drawn_currency", "last_drawn_period",
    "available_from", "notice_period_raw", "employment_type",
    "sex", "race", "race_detail", "nationality", "date_of_birth",
    "education_years",
    "notes",
)


@router.post("/candidates", status_code=201)
async def create_candidate(request: Request, body: CandidateIn) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
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
        held = await held_by_colleague(session, match, user_uuid, role)
        if held is not None:
            raise HTTPException(status_code=409, detail=held)
        if match.candidate_id is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Already recorded as candidate {match.candidate_id}",
            )

        candidate_id = uuid.uuid4()
        values = body.model_dump(exclude={"skills", "languages"})
        values.update(
            id=candidate_id,
            tenant_id=tenant_uuid,
            email=email,
            phone_e164=phone_e164,
            pipeline_stage=body.pipeline_stage or "new",
            record_status=Candidate.ACTIVE,
            # You typed it in, it is yours.
            owner_id=user_uuid,
            created_by=user_uuid,
            updated_by=user_uuid,
        )
        try:
            await session.execute(insert(Candidate).values(**values))
            await _replace_skills(session, tenant_uuid, candidate_id, body.skills or [])
            await _replace_languages(
                session, tenant_uuid, candidate_id, body.languages or []
            )
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
    if field in ("available_from", "date_of_birth"):
        return value.isoformat()
    return value


@router.patch("/candidates/{candidate_id}")
async def update_candidate(
    request: Request, candidate_id: uuid.UUID, body: CandidateUpdate
) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_editable_candidate(session, candidate_id, user_uuid, role)
        # A merged row is not a person any more; its identity belongs to the
        # target. Editing one writes to a record nothing reads, and worse, it
        # can change the email and phone that `unmerge` has to give back.
        # Archive already refuses a merged row for the same reason.
        if candidate.record_status == Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the candidate first")
        values = body.model_dump(exclude={"skills", "languages"}, exclude_unset=True)
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

        held = await patch_collision(session, tenant_uuid, candidate, values, user_uuid, role)
        if held is not None:
            raise HTTPException(status_code=409, detail=held)

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
            human_value = None if values[field] is None else str(values[field])
            # Which tier the correction lands in. A fact corrected by hand is
            # corrected for the whole agency (`user_id` NULL); a judgement is
            # this recruiter's reading and nobody else's. The split itself
            # lives in `candidate_overrides` and a test fails when a new
            # column belongs to neither set.
            is_judgement = field in JUDGEMENT_FIELDS
            override_user_id = user_uuid if is_judgement else None
            insert = pg_insert(CandidateFieldOverride).values(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                candidate_id=candidate_id,
                user_id=override_user_id,
                field_name=field,
                human_value=human_value,
                changed_by=user_uuid,
            )
            set_ = {"human_value": human_value, "changed_by": user_uuid}
            if is_judgement:
                stmt = insert.on_conflict_do_update(
                    constraint="uq_candidate_overrides_one_per_field_per_user",
                    set_=set_,
                )
            else:
                # ON CONFLICT cannot use the unique CONSTRAINT for this row:
                # its `user_id` is NULL and a NULL never collides there. The
                # partial unique index is what actually bounds the tenant-wide
                # tier, so it has to be the inference target too — naming the
                # constraint here would let the insert reach the index and
                # raise instead of updating.
                stmt = insert.on_conflict_do_update(
                    index_elements=["tenant_id", "candidate_id", "field_name"],
                    index_where=text("user_id IS NULL"),
                    set_=set_,
                )
            await session.execute(stmt)
        if body.skills is not None:
            await _replace_skills(session, tenant_uuid, candidate_id, body.skills)
        # Replace-on-write, exactly like skills: sending the list replaces it,
        # omitting it leaves it alone. An append-only list could never unsay a
        # language somebody recorded by mistake.
        if body.languages is not None:
            await _replace_languages(session, tenant_uuid, candidate_id, body.languages)
        await session.commit()

    return await get_candidate(request, candidate_id)


@router.post("/candidates/{candidate_id}/archive")
async def archive_candidate(request: Request, candidate_id: uuid.UUID) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_editable_candidate(session, candidate_id, user_uuid, role)
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
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        candidate = await load_editable_candidate(session, candidate_id, user_uuid, role)
        if candidate.record_status == Candidate.MERGED:
            raise HTTPException(status_code=400, detail="Unmerge the candidate first")
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(record_status=Candidate.ACTIVE)
        )
        await session.commit()
    return {"record_status": Candidate.ACTIVE}


@router.delete("/candidates/{candidate_id}", status_code=204)
async def delete_candidate(request: Request, candidate_id: uuid.UUID) -> Response:
    """Erase a person. Owner only, and irreversible.

    Skills, languages, overrides and activities (WhatsApp-open history — see
    `CandidateActivity`) cascade. Nothing else in phase 1 holds this person's
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


ActivityType = Literal[CandidateActivity.WHATSAPP_OPENED]
ActivityChannel = Literal[CandidateActivity.WHATSAPP]
ActivityStatus = Literal[CandidateActivity.OPENED]


class ActivityIn(BaseModel):
    activity_type: ActivityType
    channel: ActivityChannel
    message_text: str | None = None


def _serialize_activity(activity: CandidateActivity, actor_name: str) -> dict:
    return {
        "id": str(activity.id),
        "activity_type": activity.activity_type,
        "channel": activity.channel,
        "message_text": activity.message_text,
        "status": activity.status,
        # Both null on an `opened` row, and mutually exclusive on the other
        # two: the timeline shows what WhatsApp answered, or why it did not.
        "provider_message_id": activity.provider_message_id,
        "error": activity.error,
        "actor_name": actor_name,
        "created_at": activity.created_at.isoformat(),
    }


@router.post("/candidates/{candidate_id}/activities", status_code=201)
async def log_activity(request: Request, candidate_id: uuid.UUID, body: ActivityIn) -> dict:
    """Record that an outreach surface was opened — never that it was sent.

    `status` is always `CandidateActivity.OPENED`: there is no field on the
    request body for it, because nothing in this system observes a send
    (§15) and the CHECK constraint would refuse anything else anyway.
    """
    # The READ guard, deliberately: the row this writes is a
    # `candidate_activities` row, not the candidate. A share recipient may
    # record that they opened a WhatsApp chat — a fact about what they did.
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        await load_visible_candidate(session, candidate_id, user_uuid, role)
        activity_id = uuid.uuid4()
        await session.execute(
            insert(CandidateActivity).values(
                id=activity_id,
                tenant_id=tenant_uuid,
                candidate_id=candidate_id,
                user_id=user_uuid,
                activity_type=body.activity_type,
                channel=body.channel,
                message_text=body.message_text,
                status=CandidateActivity.OPENED,
            )
        )
        # Read back before commit, not after: `tenant_session` sets
        # `app.tenant_id` with SET LOCAL, which is transaction-scoped and is
        # cleared the instant the transaction commits. A select issued after
        # `session.commit()` on this same session would run with no tenant
        # set and RLS would return zero rows — not a leak, but a spurious
        # 404-shaped failure on a row that exists. `tenant_session` commits
        # for us on context exit, so this select just needs to happen first.
        activity = (
            await session.execute(
                select(CandidateActivity).where(CandidateActivity.id == activity_id)
            )
        ).scalar_one()
        user = (
            await session.execute(select(User).where(User.id == user_uuid))
        ).scalar_one()
    return _serialize_activity(
        activity, actor_name(user.preferred_name, user.display_name, user.email)
    )


@router.get("/candidates/{candidate_id}/activities")
async def list_activities(request: Request, candidate_id: uuid.UUID) -> dict:
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        await load_visible_candidate(session, candidate_id, user_uuid, role)
        rows = (
            (
                await session.execute(
                    select(CandidateActivity)
                    .where(CandidateActivity.candidate_id == candidate_id)
                    .order_by(CandidateActivity.created_at.desc())
                    .limit(settings.CANDIDATE_ACTIVITIES_PAGE_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        # One lookup for every actor on the page rather than N — a recruiter's
        # WhatsApp history is usually one person opening it repeatedly.
        actor_ids = {row.user_id for row in rows if row.user_id is not None}
        actors: dict[uuid.UUID, User] = {}
        if actor_ids:
            for user in (
                (await session.execute(select(User).where(User.id.in_(actor_ids))))
                .scalars()
                .all()
            ):
                actors[user.id] = user

    def _name_for(row: CandidateActivity) -> str:
        user = actors.get(row.user_id) if row.user_id else None
        if user is None:
            # The user row is gone (deleted) but SET NULL kept the activity —
            # honest about what we can no longer say (§15) rather than
            # inventing a name for someone the record no longer identifies.
            return "Unknown user"
        return actor_name(user.preferred_name, user.display_name, user.email)

    return {"items": [_serialize_activity(row, _name_for(row)) for row in rows]}


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


async def _replace_languages(
    session, tenant_uuid: uuid.UUID, candidate_id: uuid.UUID, languages: list
) -> None:
    """Languages are a set, replaced wholesale — `_replace_skills` exactly.

    Deduplicated on the normalised form and first-wins, so a payload naming
    "English" twice at two fluencies does not trip
    `uq_candidate_languages_once_per_candidate` with a 500. First rather than
    last is arbitrary but has to be one of them; what matters is that the row
    the recruiter sees back is one they sent.
    """
    await session.execute(
        delete(CandidateLanguage).where(CandidateLanguage.candidate_id == candidate_id)
    )
    seen: set[str] = set()
    for entry in languages:
        normalized = normalize_language(entry.language)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        await session.execute(
            insert(CandidateLanguage).values(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                candidate_id=candidate_id,
                language=entry.language.strip(),
                language_normalized=normalized,
                fluency=entry.fluency,
            )
        )
