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

# Prefix, not a single cookie name. One fixed name is unusable here: Chrome
# speculatively prerenders the "Sign in" anchor, so a single user click can
# issue two GET /auth/microsoft/login requests. Each starts its own MSAL flow,
# and with one name the second `set_cookie` silently overwrites the first —
# while the browser navigates using the *first* flow's `auth_uri`. The callback
# then arrives holding the wrong flow and 400s. Each flow therefore gets its
# own cookie, named for its `state`.
#
# Chosen over "one cookie holding a map of state -> flow": a sealed flow is
# already ~1.2 KB, and browsers drop cookies over ~4096 bytes, so a map would
# hold at most two flows before silently truncating. Separate cookies are
# limited per-cookie instead of in aggregate, and each expires on its own.
FLOW_COOKIE = "ea_ms_flow"
FLOW_COOKIE_PREFIX = f"{FLOW_COOKIE}_"

# How many in-flight flows to keep. Two covers the prerender double-fire; four
# leaves room for a user who clicks again in another tab. Beyond that the
# oldest are dropped, so a bot hammering /login cannot grow the Cookie header
# without bound.
MAX_CONCURRENT_FLOWS = 4

# The sign-in must be completed in one sitting, but "one sitting" is not ten
# minutes: a first-ever sign-in can walk through the consent screen, MFA
# enrolment (installing Authenticator, scanning a QR code) and a password
# reset before Microsoft ever issues the code. Ten minutes timed those users
# out. Thirty is long enough for that path and still far short of leaving a
# login tab open indefinitely.
FLOW_TTL_SECONDS = 30 * 60

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

# Where a completed sign-in lands. A signed-in user dropped back on the
# marketing page has no sign that anything happened. Not configuration — it is
# a route the frontend owns, so it must stay in step with `frontend/app/
# dashboard/`; renaming that route means renaming this.
DASHBOARD_PATH = "/dashboard"


def _frontend_url(path: str) -> str:
    """A URL under FRONTEND_ORIGIN, tolerating a configured trailing slash."""
    return f"{settings.FRONTEND_ORIGIN.rstrip('/')}{path}"

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


def _flow_cookie_name(state: str) -> str:
    """Cookie name for one flow, derived from its `state`.

    MSAL puts `state` in the flow dict and Microsoft echoes it back as a query
    parameter, so the callback can name the exact cookie it needs. The value is
    hashed rather than used verbatim: `state` is attacker-influenceable only in
    the callback, and a raw value there could carry characters that are illegal
    in a cookie name or, worse, smuggle a `;`. A truncated digest is a fixed,
    always-legal token. It is not a secret — the flow itself stays encrypted.
    """
    return FLOW_COOKIE_PREFIX + hashlib.sha256(state.encode()).hexdigest()[:16]


def _stale_flow_cookies(request: Request) -> list[str]:
    """Flow cookie names to evict, oldest first, to stay under the cap.

    RFC 6265 has the browser send same-path cookies in creation order, so the
    head of the list is the oldest. Ordering is a hint, not a guarantee, and
    getting it wrong only drops a flow the user is not completing anyway.
    """
    names = [n for n in request.cookies if n.startswith(FLOW_COOKIE_PREFIX)]
    # One slot is about to be taken by the flow being started.
    excess = len(names) - (MAX_CONCURRENT_FLOWS - 1)
    return names[:excess] if excess > 0 else []


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
async def microsoft_login(request: Request) -> RedirectResponse:
    """Send the browser to Microsoft, carrying the flow back in a sealed cookie."""
    _require_microsoft()
    flow = ms_auth.begin_login()
    response = RedirectResponse(flow["auth_uri"])
    for stale in _stale_flow_cookies(request):
        response.delete_cookie(stale, path="/")
    response.set_cookie(
        _flow_cookie_name(flow["state"]), _seal_flow(flow), **_cookie_kwargs(FLOW_TTL_SECONDS)
    )
    return response


@router.get("/auth/microsoft/callback")
async def microsoft_callback(request: Request) -> RedirectResponse:
    """Complete the flow, provision tenant + user, and start a session."""
    _require_microsoft()

    # Each branch below logs a distinct, stable reason code: in production they
    # were indistinguishable, which is why a prerender race took so long to
    # identify. Nothing logged here is secret — no code, token or cookie value,
    # only counts and the reason.
    state = request.query_params.get("state")
    if not state:
        log.warning("ms_callback_rejected", reason="no_state_param")
        raise HTTPException(status_code=400, detail="Sign-in did not start here, or it expired.")

    cookie_name = _flow_cookie_name(state)
    sealed = request.cookies.get(cookie_name)
    if not sealed:
        log.warning(
            "ms_callback_rejected",
            reason="no_flow_cookie_for_state",
            flow_cookies_present=sum(
                1 for n in request.cookies if n.startswith(FLOW_COOKIE_PREFIX)
            ),
        )
        raise HTTPException(status_code=400, detail="Sign-in did not start here, or it expired.")
    try:
        flow = _open_flow(sealed)
    except (InvalidToken, ValueError) as exc:
        # Undecryptable: expired past FLOW_TTL_SECONDS, or sealed under a
        # different APP_SECRET_KEY (a key rotation, or a replica mid-rollout).
        log.warning("ms_callback_rejected", reason="flow_cookie_unsealable")
        raise HTTPException(status_code=400, detail="Sign-in state is invalid or expired.") from exc

    # MSAL validates state, nonce and the id_token signature in here. It raises
    # ValueError — not an error result — when the state does not match or the
    # callback is replayed, which is a bad request, not a server fault.
    try:
        result = ms_auth.complete_login(flow, dict(request.query_params))
    except ValueError as exc:
        log.warning("ms_callback_rejected", reason="msal_rejected_flow")
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

    response = RedirectResponse(_frontend_url(DASHBOARD_PATH))
    response.set_cookie(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_uuid)}),
        **_cookie_kwargs(SESSION_TTL_SECONDS),
    )
    # Clear every in-flight flow, not just the one just completed: the others
    # are abandoned siblings from the same click and hold live PKCE verifiers.
    for name in request.cookies:
        if name.startswith(FLOW_COOKIE_PREFIX):
            response.delete_cookie(name, path="/")
    return response


@router.post("/auth/logout")
async def logout(response: Response) -> dict[str, str]:
    """Clearing the cookie is the whole logout — no server-side session exists."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "signed-out"}


@router.get("/auth/me")
async def me(request: Request) -> dict[str, dict]:
    """The signed-in user, their tenant, and their mailbox connection state.

    Grouped rather than flat because the dashboard renders onboarding from it:
    a Microsoft user with no mailbox row and a personal-account user with no
    colleagues need different prompts, and both are decided here (§6.1).
    """
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
        if user is None:
            # A deleted user with a live cookie must not look signed in.
            raise HTTPException(status_code=401, detail="Not signed in.")

        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))
        ).scalar_one()

        # Only the scope is read, never the ciphertext: the row's existence is
        # the whole signal. Decrypting would put a live refresh token in memory
        # to answer a question its presence already answers, and a token that
        # Entra has since revoked still decrypts fine — so decryption would not
        # even make the answer more truthful.
        # The id comes back too, so a row whose `scope` is null still counts as
        # connected rather than being mistaken for a missing row.
        token = (
            await session.execute(
                select(MicrosoftToken.id, MicrosoftToken.scope).where(
                    MicrosoftToken.user_id == user.id
                )
            )
        ).one_or_none()
        connected = token is not None
        scope = token.scope if token else None

    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
        },
        "tenant": {
            "id": str(tenant.id),
            "name": tenant.name,
            "is_personal_account": tenant.is_personal_account,
        },
        "mailbox": {
            # Microsoft Graph is the only ingestion path; a Google identity has
            # no mailbox to connect at all.
            "provider": "microsoft",
            "connected": connected,
            "scopes": (scope or "").split(),
            # Hardcoded until ingestion exists (§7). It must become a real read
            # of the ingestion subscription state when that ships — a connected
            # mailbox is not the same as one being polled.
            "ingestion_active": False,
        },
    }
