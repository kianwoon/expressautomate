"""Microsoft Entra ID sign-in endpoints (plan §6.1).

Routes are declared **unprefixed**: Koyeb strips the matched `/api` route
prefix before forwarding, so `/auth/microsoft/login` here is
`https://expressautomate.app/api/auth/microsoft/login` in public. See
`tests/test_routing.py`.
"""

import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.crypto import encrypt
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models import MicrosoftToken, Tenant, User
from app.services import ms_auth

log = get_logger(__name__)
router = APIRouter(tags=["auth"])

SESSION_COOKIE = "ea_session"
FLOW_COOKIE = "ea_ms_flow"

# A sign-in must be completed promptly; the login page is not a place to leave
# a tab open for an hour.
FLOW_TTL_SECONDS = 10 * 60

# Every personal Microsoft account (outlook.com, hotmail.com, live.com) reports
# this one shared `tid`. It is a fixed Microsoft protocol constant, not
# configuration. Since the Entra tenant GUID is otherwise *the* tenant primary
# key, using it verbatim would merge all consumer sign-ins worldwide into a
# single tenant, where RLS would then happily show them each other's users and
# refresh tokens. `_tenant_for` therefore never keys a tenant on this GUID.
MSA_CONSUMER_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"

# Namespace for the per-user tenant ids derived for personal accounts. Any fixed
# UUID works; it exists so the derivation is deterministic (same `oid` → same
# tenant on every sign-in) and confined to a namespace that no Entra-issued
# tenant GUID can be drawn from in practice.
PERSONAL_TENANT_NAMESPACE = uuid.UUID("6f1f7f7a-6b1e-5a6d-9d2a-7c4f1c9f0b3e")

SESSION_TTL_SECONDS = 14 * 24 * 60 * 60

_session_serializer = URLSafeTimedSerializer(settings.APP_SECRET_KEY, salt=SESSION_COOKIE)


def _require_microsoft() -> None:
    """Missing credentials are an operator problem, not a client error."""
    if not settings.microsoft_configured():
        raise HTTPException(
            status_code=503,
            detail="Microsoft sign-in is not configured on this deployment "
            "(MS_CLIENT_ID / MS_CLIENT_SECRET). See docs/setup.md.",
        )


def _cookie_kwargs(max_age: int) -> dict:
    return {
        "max_age": max_age,
        "httponly": True,
        "samesite": "lax",
        # Localhost development is plain HTTP, where a Secure cookie is dropped.
        "secure": settings.is_production,
        "path": "/",
    }


# --- the MSAL flow dict -----------------------------------------------------
#
# It is held in a cookie rather than server-side state. The API runs as
# stateless replicas behind Koyeb with no shared store wired up yet, so a
# server-side flow store would need sticky sessions or Redis to survive a
# rollout mid-login. The dict contains the PKCE verifier and nonce — secrets,
# not merely tamper-sensitive values — so it is *encrypted* with Fernet rather
# than signed: signing would leave the verifier readable to anything that gets
# at the cookie. Fernet's own timestamp gives the TTL.


@lru_cache(maxsize=1)
def _flow_cipher() -> Fernet:
    """A Fernet key derived from APP_SECRET_KEY, so no new secret is needed.

    APP_SECRET_KEY is arbitrary text and Fernet demands 32 url-safe base64
    bytes; hashing bridges the two. TOKEN_ENCRYPTION_KEY is deliberately not
    reused — that key protects data at rest and should not also be handed to a
    value that travels to the browser.
    """
    digest = hashlib.sha256(settings.APP_SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _seal_flow(flow: dict) -> str:
    return _flow_cipher().encrypt(json.dumps(flow).encode()).decode()


def _open_flow(sealed: str) -> dict:
    raw = _flow_cipher().decrypt(sealed.encode(), ttl=FLOW_TTL_SECONDS)
    return json.loads(raw)


def _slug_for(tenant_uuid: uuid.UUID, stem_source: str) -> str:
    """A readable, collision-free slug.

    Entra gives no tenant name in the token, and two agencies can share a
    vanity domain, so the tenant id is folded in to keep the unique index safe.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", stem_source.lower()).strip("-")[:60] or "tenant"
    return f"{stem}-{tenant_uuid.hex[:8]}"


def _tenant_for(tid: str, oid: str, email: str) -> tuple[uuid.UUID, str, str, bool]:
    """Resolve (tenant id, name, slug, is_personal_account) from the id_token.

    Work/school account: the Entra tenant GUID *is* the tenant primary key, so
    colleagues from one agency share a tenant — the intended multi-recruiter
    case. Keying on the claim also makes the very first statement scopable, so
    the bootstrap runs under the RLS policy instead of around it.

    Personal account: every consumer account in the world shares one `tid`, so
    that GUID is unusable as a key. Each such user instead gets a private
    tenant whose id is a UUIDv5 over their own immutable `oid` — deterministic,
    so re-signing in resolves to the same tenant, and unique per user. Naming
    it after the mail domain would label every one of them "outlook.com", so
    the local part is used instead.
    """
    is_personal = tid.lower() == MSA_CONSUMER_TENANT_ID
    local_part, _, domain = email.partition("@")
    if is_personal:
        tenant_uuid = uuid.uuid5(PERSONAL_TENANT_NAMESPACE, oid)
        name = local_part.lower()
    else:
        try:
            tenant_uuid = uuid.UUID(tid)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="Unrecognised Microsoft tenant.") from exc
        name = domain.lower()
    return tenant_uuid, name or "tenant", _slug_for(tenant_uuid, name), is_personal


@router.get("/auth/microsoft/login")
async def microsoft_login() -> RedirectResponse:
    """Send the browser to Microsoft, carrying the flow back in a sealed cookie."""
    _require_microsoft()
    flow = ms_auth.begin_login()
    response = RedirectResponse(flow["auth_uri"])
    response.set_cookie(FLOW_COOKIE, _seal_flow(flow), **_cookie_kwargs(FLOW_TTL_SECONDS))
    return response


@router.get("/auth/microsoft/callback")
async def microsoft_callback(request: Request) -> RedirectResponse:
    """Complete the flow, provision tenant + user, and start a session."""
    _require_microsoft()

    sealed = request.cookies.get(FLOW_COOKIE)
    if not sealed:
        raise HTTPException(status_code=400, detail="Sign-in did not start here, or it expired.")
    try:
        flow = _open_flow(sealed)
    except (InvalidToken, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Sign-in state is invalid or expired.") from exc

    # MSAL validates state, nonce and the id_token signature in here. It raises
    # ValueError — not an error result — when the state does not match or the
    # callback is replayed, which is a bad request, not a server fault.
    try:
        result = ms_auth.complete_login(flow, dict(request.query_params))
    except ValueError as exc:
        log.warning("ms_login_rejected_flow")
        raise HTTPException(status_code=400, detail="Sign-in state is invalid or expired.") from exc

    if "id_token_claims" not in result:
        # `error_description` can echo back user input; log the stable code only.
        log.warning("ms_login_failed", error=result.get("error"))
        raise HTTPException(status_code=401, detail="Microsoft rejected the sign-in.")

    claims = result["id_token_claims"]
    tid = claims.get("tid")
    oid = claims.get("oid")
    email = claims.get("preferred_username") or claims.get("email")
    if not (tid and oid and email):
        raise HTTPException(status_code=401, detail="Microsoft returned an incomplete profile.")

    tenant_uuid, name, slug, is_personal = _tenant_for(str(tid), str(oid), email)

    async with tenant_session(tenant_uuid) as session:
        await session.execute(
            pg_insert(Tenant)
            .values(
                id=tenant_uuid,
                name=name,
                slug=slug,
                # The real MSA tid is still recorded; only the primary key is
                # derived, so downstream Graph code sees the authority it needs.
                ms_tenant_id=str(tid),
                is_personal_account=is_personal,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )

        now = datetime.now(UTC)
        user_id = (
            await session.execute(
                pg_insert(User)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_uuid,
                    email=email,
                    display_name=claims.get("name"),
                    ms_object_id=str(oid),
                    role="recruiter",
                    last_login_at=now,
                )
                # Identity is the immutable Entra object id, never the mail
                # address: a UPN rename must update this user, not create a
                # second one, and an address reassigned to someone new must not
                # inherit the previous holder's row and refresh token.
                .on_conflict_do_update(
                    constraint="uq_users_tenant_ms_object_id",
                    set_={
                        "email": email,
                        "display_name": claims.get("name"),
                        "last_login_at": now,
                        "updated_at": now,
                    },
                )
                .returning(User.id)
            )
        ).scalar_one()

        refresh_token = result.get("refresh_token")
        if refresh_token:
            ciphertext = encrypt(refresh_token)
            await session.execute(
                pg_insert(MicrosoftToken)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_uuid,
                    user_id=user_id,
                    # MSAL's own account key format: "<oid>.<tid>".
                    home_account_id=f"{oid}.{tid}",
                    refresh_token_encrypted=ciphertext,
                    scope=result.get("scope"),
                )
                .on_conflict_do_update(
                    constraint="uq_ms_tokens_tenant_user",
                    set_={
                        "refresh_token_encrypted": ciphertext,
                        "scope": result.get("scope"),
                        "updated_at": now,
                    },
                )
            )

    log.info("ms_login", tenant_id=str(tenant_uuid), user_id=str(user_id))

    response = RedirectResponse(settings.FRONTEND_ORIGIN)
    response.set_cookie(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_uuid)}),
        **_cookie_kwargs(SESSION_TTL_SECONDS),
    )
    response.delete_cookie(FLOW_COOKIE, path="/")
    return response


@router.post("/auth/logout")
async def logout(response: Response) -> dict[str, str]:
    """Clearing the cookie is the whole logout — no server-side session exists."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "signed-out"}


@router.get("/auth/me")
async def me(request: Request) -> dict[str, str | None]:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise HTTPException(status_code=401, detail="Not signed in.")
    try:
        payload = _session_serializer.loads(cookie, max_age=SESSION_TTL_SECONDS)
        tenant_uuid = uuid.UUID(payload["tid"])
        user_uuid = uuid.UUID(payload["uid"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Session is invalid or expired.") from exc

    async with tenant_session(tenant_uuid) as session:
        user = (
            await session.execute(select(User).where(User.id == user_uuid))
        ).scalar_one_or_none()

    # A deleted user with a live cookie must not look signed in.
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in.")

    return {
        "id": str(user.id),
        "tenant_id": str(user.tenant_id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
    }
