"""Identity Resolver routes — Serper-backed candidate identity resolution.

Spec: `serper design.md` §27 (the flow), §34 (the cache), §42 (backend-only
key), §56/§57 (endpoint + response).

Three routes, all naming an `opportunity_id` and all gated by
`load_visible_opportunity`, for the same reason `external_candidates.py` is
(the AST test in `tests/test_opportunity_routes_guarded.py` enforces it). The
POST takes the read-guard-only exemption `run_intelligence` takes: a share
recipient may resolve a candidate on work shown to them — visibility, not
edit rights.

The candidate's attributes arrive in the request body, not from a local row:
external candidates live inside an opportunity's `external_candidate_searches.
results` JSONB and have no table of their own, so the fingerprint is built
from `{candidate: {name, current_company, current_title, location,
previous_companies}}`. The resolver validates only that a name is present — a
name-less fingerprint cannot resolve anything and is refused early.

**The Serper key never reaches these routes.** `serper_provider()` is a FastAPI
dependency so tests substitute a fake provider; the key is read inside
`SerperSearchProvider`, sent as a header, and never logged (§42).
"""

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate_identity_resolution import CandidateIdentityResolution
from app.services import identity_resolver
from app.services.identity_resolver import Fingerprint
from app.services.serper import (
    SerperError,
    SerperSearchProvider,
    SerperUnconfiguredError,
    WebSearchProvider,
)
from app.services.visibility import load_visible_opportunity

log = get_logger(__name__)

router = APIRouter(tags=["identity_resolutions"])

# Structured, never-a-500 statuses the panel renders as sentences — the same
# shape the external-candidates routes answer with.
STATUS_UNCONFIGURED = "unconfigured"
STATUS_UNREACHABLE = "unreachable"
STATUS_NOT_PROVISIONED = "not_provisioned"

# Modes accepted by the POST (§56): `normal`/`refresh`/`deep`. A bare body or
# no body is `normal`.
_MODES = {"normal", "refresh", "deep"}

_MISSING_TABLE = "42P01"


class CandidateFingerprint(BaseModel):
    """The candidate attributes the resolver fingerprints (§6).

    `name` is the only required field: §5 says not all fields exist, and a
    fingerprint without a name cannot produce a query. Extra fields are
    ignored rather than rejected — the browser sends a whole result object.
    """

    name: str = Field(min_length=1)
    current_company: str | None = None
    current_title: str | None = None
    location: str | None = None
    previous_companies: list[str] = Field(default_factory=list)


class ResolveIdentityBody(BaseModel):
    mode: str = "normal"
    candidate: CandidateFingerprint


def serper_provider() -> WebSearchProvider:
    """The search provider, as a dependency so tests substitute the double.

    A FastAPI dependency rather than a module global: `app.dependency_
    overrides` cannot reach a global, and a test asserting the resolver's
    scoring must never make a live Serper call. The default reads the real key
    from settings; when it is unset, `SerperSearchProvider.search` raises
    `SerperUnconfiguredError`, which the route maps to a structured answer.
    """
    return SerperSearchProvider()


def _serialize(row: CandidateIdentityResolution) -> dict[str, Any]:
    """The §57 response shape, plus the columns the panel needs.

    `status`/`confidence`/`canonical_profile_url`/`queries_used`/
    `freshness_status`/`evidence` are §57 verbatim; the rest describe the
    stored row (`id`, `created_at`, `expires_at`) so a modal reopened from
    history renders identically to a fresh resolve.
    """
    return {
        "id": str(row.id),
        "candidate_key": row.candidate_key,
        "status": row.status,
        "confidence": row.confidence,
        "canonical_profile_url": row.canonical_profile_url,
        "current_company": row.current_company,
        "current_title": row.current_title,
        "location": row.location,
        "previous_companies": row.previous_companies or [],
        "evidence": row.evidence or [],
        "queries_used": row.queries_used,
        "freshness_status": row.freshness_status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def _not_provisioned(exc: Exception) -> dict[str, Any]:
    log.error("identity_resolution_schema_missing", error=str(exc))
    return {
        "status": STATUS_NOT_PROVISIONED,
        "message": (
            "Identity resolution is not provisioned in this deployment's "
            "database yet — an administrator needs to run the pending "
            "migrations."
        ),
    }


async def _unexpired_cache(
    session, opportunity_id: uuid.UUID, candidate_key: str, fingerprint_hash: str
) -> CandidateIdentityResolution | None:
    """The newest unexpired resolution for this fingerprint (§34).

    Keyed on all three of opportunity, candidate and fingerprint hash: the
    fingerprint hash alone would let a candidate resolved for job A be shown
    for job B; the candidate key alone would show a result for a fingerprint
    the recruiter has since edited. `expires_at` is §34's TTL, written at
    resolve time.
    """
    now = dt.datetime.now(dt.UTC)
    return (
        await session.execute(
            select(CandidateIdentityResolution)
            .where(
                CandidateIdentityResolution.opportunity_id == opportunity_id,
                CandidateIdentityResolution.candidate_key == candidate_key,
                CandidateIdentityResolution.fingerprint_hash == fingerprint_hash,
                CandidateIdentityResolution.expires_at > now,
            )
            .order_by(CandidateIdentityResolution.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.post(
    "/opportunities/{opportunity_id}/candidates/{candidate_key}/resolve-identity"
)
async def resolve_identity(
    request: Request,
    opportunity_id: uuid.UUID,
    candidate_key: str,
    body: ResolveIdentityBody,
    provider: WebSearchProvider = Depends(serper_provider),
) -> dict:
    """Resolve one external candidate's identity (§27, §56).

    Read-guard-only exemption from the EDIT assertion, like `run_intelligence`:
    a share recipient may resolve a candidate on work shown to them. The row
    written is a `candidate_identity_resolutions` row, not the job order.

    A cache hit (§34) returns the stored row without a Serper call. A
    `refresh`/`deep` run bypasses the cache by design — those are the modes a
    recruiter reaches for *because* they distrust the cached answer. When
    Serper is unconfigured or unreachable the resolver still produces a result
    from whatever the provider returns; an unconfigured provider is the one
    case answered with a structured status rather than an empty UNRESOLVED.
    """
    if body.mode not in _MODES:
        raise HTTPException(status_code=422, detail="Unknown resolution mode.")
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    fingerprint = Fingerprint.from_body(body.candidate.model_dump())
    fingerprint_hash = fingerprint.hash()
    bypass_cache = body.mode in ("refresh", "deep")

    async with tenant_session(tenant_uuid) as session:
        try:
            current = await load_visible_opportunity(
                session, opportunity_id, user_uuid, role
            )
            if not bypass_cache:
                cached = await _unexpired_cache(
                    session, current.id, candidate_key, fingerprint_hash
                )
                if cached is not None:
                    return {**_serialize(cached), "cached": True}
        except HTTPException:
            raise
        except DBAPIError as exc:
            if _sqlstate(exc) == _MISSING_TABLE:
                return _not_provisioned(exc)
            raise

    # The resolver runs outside the tenant session on purpose: it makes
    # network calls, and holding a transaction open across a Serper round trip
    # (up to 8 of them) would pin a database connection for the duration. All
    # authorisation already happened above.
    try:
        resolution = await identity_resolver.resolve(
            fingerprint, provider, mode=body.mode
        )
    except SerperUnconfiguredError:
        return {
            "status": STATUS_UNCONFIGURED,
            "message": (
                "Identity resolution is not set up for this deployment — an "
                "administrator needs to configure the web search provider."
            ),
        }
    except SerperError:
        return {
            "status": STATUS_UNREACHABLE,
            "message": (
                "The web search provider could not be reached. Try again in a "
                "moment."
            ),
        }

    log.info(
        "identity_resolution_done",
        candidate_key=candidate_key,
        provider="serper",
        mode=body.mode,
        queries_used=resolution.queries_used,
        confidence=resolution.confidence,
        status=resolution.status,
    )

    row = CandidateIdentityResolution(
        tenant_id=tenant_uuid,
        opportunity_id=current.id,
        candidate_key=candidate_key,
        fingerprint=fingerprint.as_dict(),
        fingerprint_hash=fingerprint_hash,
        status=resolution.status,
        confidence=resolution.confidence,
        canonical_profile_url=resolution.canonical_profile_url,
        current_company=resolution.current_company,
        current_title=resolution.current_title,
        location=resolution.location,
        previous_companies=resolution.previous_companies,
        evidence=[e.as_dict() for e in resolution.evidence],
        queries_used=resolution.queries_used,
        freshness_status=resolution.freshness_status,
        expires_at=identity_resolver.expires_at(resolution.status),
        created_by=user_uuid,
    )

    try:
        async with tenant_session(tenant_uuid) as session:
            session.add(row)
            await session.commit()
    except DBAPIError as exc:
        # A persistence miss must not fail the read: the resolver already has
        # the answer in hand, and the recruiter can see it; the only cost is
        # that reopening the modal re-runs the search. Same reading
        # `_persist_terminal` takes in external_candidates.py.
        if _sqlstate(exc) == _MISSING_TABLE:
            return _not_provisioned(exc)
        log.warning(
            "identity_resolution_persist_failed",
            candidate_key=candidate_key,
            error=str(exc),
            exc_info=True,
        )

    return {**_serialize(row), "cached": False}


@router.get("/opportunities/{opportunity_id}/identity-resolutions")
async def list_identity_resolutions(
    request: Request, opportunity_id: uuid.UUID
) -> dict:
    """History for one job order, newest first (§28's past-results list).

    Read-guarded like every by-id opportunity route. Only the list columns are
    returned — the full evidence is fetched per row from the `{resId}` route,
    so the list stays small however many resolutions accumulate.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        try:
            current = await load_visible_opportunity(
                session, opportunity_id, user_uuid, role
            )
            rows = (
                await session.execute(
                    select(CandidateIdentityResolution)
                    .where(
                        CandidateIdentityResolution.opportunity_id == current.id
                    )
                    .order_by(
                        CandidateIdentityResolution.created_at.desc(),
                        CandidateIdentityResolution.id.desc(),
                    )
                )
            ).scalars().all()
        except HTTPException:
            raise
        except DBAPIError as exc:
            if _sqlstate(exc) == _MISSING_TABLE:
                return _not_provisioned(exc)
            raise

    now = dt.datetime.now(dt.UTC)
    return {
        "status": "ok",
        "resolutions": [
            {
                "id": str(row.id),
                "candidate_key": row.candidate_key,
                "status": row.status,
                "confidence": row.confidence,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "expired": row.expires_at <= now if row.expires_at else True,
            }
            for row in rows
        ],
    }


@router.get("/opportunities/{opportunity_id}/identity-resolutions/{resolution_id}")
async def get_identity_resolution(
    request: Request, opportunity_id: uuid.UUID, resolution_id: uuid.UUID
) -> dict:
    """One stored resolution in full — what the modal reopens (§28).

    404 for "no such resolution" and "not this job order's" alike, the same
    unprobeable answer `_TaskGate` gives: a resolution id alone must not be a
    way to read another job order's evidence.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        try:
            current = await load_visible_opportunity(
                session, opportunity_id, user_uuid, role
            )
            row = (
                await session.execute(
                    select(CandidateIdentityResolution).where(
                        CandidateIdentityResolution.id == resolution_id,
                        CandidateIdentityResolution.opportunity_id == current.id,
                    )
                )
            ).scalar_one_or_none()
        except HTTPException:
            raise
        except DBAPIError as exc:
            if _sqlstate(exc) == _MISSING_TABLE:
                return _not_provisioned(exc)
            raise
    if row is None:
        raise HTTPException(status_code=404, detail="Resolution not found.")
    return {"status": "ok", "resolution": _serialize(row)}


def _sqlstate(exc: DBAPIError) -> str | None:
    """SQLSTATE 42P01: the feature's table is absent — a deployment fault, not
    a request error (see external_candidates.py for the precedent)."""
    return getattr(getattr(exc, "orig", None), "sqlstate", None)
