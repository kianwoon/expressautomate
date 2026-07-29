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
from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.crypto import encrypt
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models import MicrosoftToken, Tenant, User
from app.services import ms_auth
from app.services.events import KIND_MAILBOX, publish
from app.workers.queue import enqueue

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

# OIDC `prompt` values this deployment will forward to Microsoft. A whitelist,
# not a passthrough: the value lands in the authorize URL the browser is
# redirected to, and an unchecked query parameter there is parameter injection
# waiting to happen. Only the one value the frontend actually sends is listed —
# `login` and `consent` are valid OIDC but nothing here asks for them, and an
# unused entry is only extra surface. `none` would be wrong in any case: it
# fails outright when interaction is needed, which is not a sign-in button's job.
ALLOWED_PROMPTS = frozenset({"select_account"})

# Where a completed sign-in lands. A signed-in user dropped back on the
# marketing page has no sign that anything happened. Not configuration — it is
# a route the frontend owns, so it must stay in step with `frontend/app/
# dashboard/`; renaming that route means renaming this.
DASHBOARD_PATH = "/dashboard"


def _iso(value: datetime | None) -> str | None:
    """Timestamps cross the wire as ISO strings or not at all.

    None is meaningful here and must survive: "no mail yet" and "the oldest is
    from January" are different answers, and a zero or an epoch would read as
    the second.
    """
    return value.isoformat() if value else None


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


# The two things a round trip to Microsoft can be for. They share one redirect
# URI — Entra registrations list them explicitly and every extra one is another
# thing to keep in step across environments — so the purpose has to travel with
# the flow. It rides inside the sealed cookie rather than the callback URL:
# there it is encrypted and tamper-proof, whereas a query parameter would let
# anyone relabel a sign-in as a mailbox consent, or the reverse.
PURPOSE_SIGN_IN = "sign-in"
PURPOSE_MAILBOX = "mailbox"


def _seal_flow(flow: dict, purpose: str, session: dict | None = None) -> str:
    """Seal the MSAL flow together with what it is for.

    The MSAL dict is nested rather than merged with our own keys: it must reach
    `complete_login` exactly as MSAL produced it, and nesting makes that
    impossible to get wrong by accident.
    """
    return (
        _flow_cipher()
        .encrypt(json.dumps({"msal": flow, "purpose": purpose, "session": session}).encode())
        .decode()
    )


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


def _require_session(request: Request) -> tuple[uuid.UUID, uuid.UUID]:
    """(user id, tenant id) from the session cookie, or 401.

    Only the cookie is checked here; whether the user still exists is a
    database question and is left to callers that touch the database anyway.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise HTTPException(status_code=401, detail="Not signed in.")
    try:
        payload = _session_serializer.loads(cookie, max_age=SESSION_TTL_SECONDS)
        return uuid.UUID(payload["uid"]), uuid.UUID(payload["tid"])
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Session is invalid or expired.") from exc


def _granted(scope: str | None) -> set[str]:
    """The granted scopes, lowercased.

    Microsoft echoes back what it actually granted, in its own casing and often
    including the reserved scopes MSAL adds — so every comparison against it is
    a set comparison on lowercased names, never a string match.
    """
    return {s.lower() for s in (scope or "").split()}


def _covers_mailbox(scope: str | None) -> bool:
    """Does this grant actually carry the mailbox permissions?

    Compared against the configured mailbox scopes rather than a literal
    permission name: which permissions ingestion needs is deployment config
    (§7), and hardcoding one here would quietly lie the day that list changes.
    """
    wanted = {s.lower() for s in ms_auth.delegated(settings.mailbox_scopes)}
    return bool(wanted) and wanted <= _granted(scope)


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
async def microsoft_login(request: Request, prompt: str | None = None) -> RedirectResponse:
    """Send the browser to Microsoft, carrying the flow back in a sealed cookie.

    `?prompt=select_account` is how switching accounts is done. Microsoft, given
    no prompt, reuses the browser's existing SSO session without asking — so a
    user who has ever signed in is silently returned as the same account, and
    signing out here (which only drops our own cookie) does not change that.
    The frontend therefore asks for the picker whenever the user is choosing an
    account rather than resuming one.
    """
    _require_microsoft()
    if prompt is not None and prompt not in ALLOWED_PROMPTS:
        # Rejected rather than ignored: a silently dropped prompt would send the
        # user round the loop into the very account they were trying to leave.
        raise HTTPException(status_code=400, detail="Unsupported prompt.")
    flow = ms_auth.begin_login(ms_auth.identity_scopes(), prompt=prompt)
    return _redirect_into_flow(request, flow, PURPOSE_SIGN_IN)


@router.get("/auth/microsoft/connect-mailbox")
async def microsoft_connect_mailbox(request: Request) -> RedirectResponse:
    """Ask, separately, for the permissions mailbox ingestion needs.

    Split out of sign-in because the two face different consent bars: a tenant
    on Microsoft's recommended policy lets a user consent to identity scopes
    but not to mailbox ones, and asking for both at once made signing in
    impossible rather than making ingestion unavailable. Here the wall, if it
    comes, arrives at the point the user asked for mail to be read — which is
    also the point at which asking an administrator makes sense to them.

    Requires a session: this consent is recorded against a specific user, so
    there has to be one before the round trip starts, not after.
    """
    _require_microsoft()
    user_id, tenant_id = _require_session(request)
    flow = ms_auth.begin_login(ms_auth.mailbox_scopes())
    return _redirect_into_flow(
        request, flow, PURPOSE_MAILBOX, {"uid": str(user_id), "tid": str(tenant_id)}
    )


def _redirect_into_flow(
    request: Request, flow: dict, purpose: str, session: dict | None = None
) -> RedirectResponse:
    """Send the browser to Microsoft, carrying the sealed flow back in a cookie."""
    response = RedirectResponse(flow["auth_uri"])
    for stale in _stale_flow_cookies(request):
        response.delete_cookie(stale, path="/")
    response.set_cookie(
        _flow_cookie_name(flow["state"]),
        _seal_flow(flow, purpose, session),
        **_cookie_kwargs(FLOW_TTL_SECONDS),
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
        envelope = _open_flow(sealed)
        flow = envelope["msal"]
    except (InvalidToken, ValueError, KeyError, TypeError) as exc:
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

    if envelope.get("purpose") == PURPOSE_MAILBOX:
        return await _store_mailbox_consent(
            request, envelope, result, tenant_uuid, str(oid), str(tid)
        )

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

        # The person who signs in first is the one who set the agency up, and
        # is the only one who can delete a candidate.
        #
        # The lock is taken on the TENANT row, not on `users`. Two colleagues
        # signing in for the first time simultaneously both see an empty
        # `users` table, and `SELECT ... FROM users ... FOR UPDATE` locks
        # nothing at all when it matches no rows — so both would be made
        # owner. The tenant row exists by this point (it is upserted just
        # above), so locking it serialises the two transactions and the second
        # one sees the first one's user.
        await session.execute(
            text("SELECT 1 FROM tenants WHERE id = :t FOR UPDATE"), {"t": tenant_uuid}
        )
        existing = (
            await session.execute(
                text("SELECT 1 FROM users WHERE tenant_id = :t LIMIT 1"),
                {"t": tenant_uuid},
            )
        ).first()
        role = "recruiter" if existing else "owner"

        user_id = (
            await session.execute(
                pg_insert(User)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_uuid,
                    email=email,
                    display_name=claims.get("name"),
                    ms_object_id=str(oid),
                    role=role,
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

        # Sign-in now asks only for identity, so the refresh token it brings
        # back cannot mint a mailbox token. Storing it over one that can would
        # silently disconnect a mailbox the user connected earlier — ingestion
        # would keep a row saying "connected" and a credential that cannot
        # read mail. So a sign-in only writes the token when it does not
        # narrow what is already held.
        await _store_token_if_not_a_downgrade(
            session, result, tenant_uuid, user_id, f"{oid}.{tid}", now
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


async def _store_token_if_not_a_downgrade(
    session,
    result: dict,
    tenant_uuid: uuid.UUID,
    user_id: uuid.UUID,
    home_account_id: str,
    now: datetime,
    authoritative: bool = False,
) -> bool:
    """Upsert the refresh token unless it would narrow the stored grant.

    Returns whether it was written. Read-then-write inside the caller's
    transaction rather than a conditional `ON CONFLICT`: the comparison is a
    set relation over scope names, which is far clearer in Python than as SQL.

    `authoritative` says the caller asked for everything, so whatever came back
    IS the current grant and replaces what is stored. The mailbox flow sets it,
    and without it there is a dead end: if the configured mailbox scopes are
    ever narrowed, or Microsoft grants a subset, no later consent could satisfy
    the comparison and the refresh token could never be rotated again — leaving
    the only recovery a hand-deleted row once it expires.
    """
    refresh_token = result.get("refresh_token")
    if not refresh_token:
        return False

    scope = result.get("scope")
    # Locked, not merely read: a sign-in racing a mailbox consent could
    # otherwise read the pre-consent scope, judge itself not a downgrade, and
    # overwrite the mailbox token — precisely the loss this guard exists to
    # prevent. The row is per (tenant, user) and both flows for one user
    # contend on it, so the lock is uncontended in the ordinary case.
    existing = (
        await session.execute(
            select(MicrosoftToken.scope)
            .where(MicrosoftToken.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    # `existing is None` is the first grant; otherwise write only if the new
    # grant is at least everything the old one had.
    if not authoritative and existing is not None and not _granted(existing) <= _granted(scope):
        log.info("ms_token_kept", user_id=str(user_id), reason="narrower_grant_offered")
        return False

    ciphertext = encrypt(refresh_token)
    await session.execute(
        pg_insert(MicrosoftToken)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_uuid,
            user_id=user_id,
            # MSAL's own account key format: "<oid>.<tid>".
            home_account_id=home_account_id,
            refresh_token_encrypted=ciphertext,
            scope=scope,
        )
        .on_conflict_do_update(
            constraint="uq_ms_tokens_tenant_user",
            set_={
                "refresh_token_encrypted": ciphertext,
                "scope": scope,
                "updated_at": now,
            },
        )
    )
    return True


# allow-hardcode: SQL statements, not a phrase list.
# One round trip for everything the dashboard shows about a mailbox.
#
# Counted from the rows, never inferred. "142 emails" that came from anywhere
# but `email_messages` would be exactly the invented figure this product
# promises not to produce (§15) — and a dashboard claiming ingestion is running
# with nothing behind the claim is the same failure in a smaller font.
_MAILBOX_STATE = text(
    """
    SELECT
        -- Across every mailbox this user owns, not just one. A user can hold
        -- several (the constraint is per folder), and reporting only the first
        -- would show a healthy dashboard while a second one had stopped —
        -- exactly the silence this endpoint exists to break. Worst state wins:
        -- something needing attention outranks something working.
        min(
            CASE m.status
                WHEN 'needs_reauth' THEN 1
                WHEN 'disconnected' THEN 2
                ELSE 3
            END
        ) AS status_rank,
        bool_or(
            m.status = 'active'
            AND EXISTS (
                SELECT 1 FROM graph_subscriptions s
                WHERE s.mailbox_id = m.id AND s.status = 'active'
            )
        ) AS subscribed,
        bool_or(m.status = 'active') AS any_active,
        count(e.id) AS total,
        -- Emails that finished extraction. NOT the number of vacancies: one
        -- email can carry six, and it did. Labelling this "job orders found"
        -- put 3 above a table listing 7, which is the dashboard contradicting
        -- itself about the only figure on it anyone cares about. The vacancy
        -- count is `opportunities` below, read from the table the page renders.
        count(*) FILTER (WHERE e.processing_status = 'extracted') AS emails_extracted,
        -- A subquery, not a join. Joining `opportunities` here would repeat an
        -- email row once per vacancy it produced, so the six-vacancy email
        -- would have inflated `total`, `awaiting_extraction` and every other
        -- count on this dashboard — fixing one wrong number by corrupting the
        -- rest.
        --
        -- Scoped by the tenant policy alone, deliberately NOT by `user_id`
        -- like the counts above it. Those describe this user's mailboxes;
        -- this one has to match `GET /opportunities`, which lists everything
        -- the agency has found. Filtering by user here would put a colleague's
        -- vacancies in the table and not in the count — the same two-numbers-
        -- disagreeing bug this commit exists to fix, one tier up — and would
        -- hide the table entirely from a recruiter whose own mailbox happened
        -- to yield nothing.
        (SELECT count(*) FROM opportunities) AS opportunities,
        -- Two buckets, not one. "In progress" used to cover both, and the
        -- dashboard rendered it as "still processing — usually seconds each"
        -- while 82 emails sat at `fetched` waiting for an extraction stage
        -- that is not deployed. Work in flight and work parked are different
        -- facts and the difference is the whole honesty of the number.
        count(*) FILTER (WHERE e.processing_status = 'pending') AS in_progress,
        count(*) FILTER (
            -- `classified` belongs here for the same reason the bucket exists:
            -- a verdicted email whose extraction has not run is parked work,
            -- and omitting it would drop those emails out of every count on
            -- the dashboard while the row still sat in the table.
            WHERE e.processing_status IN ('fetched', 'classifying', 'classified',
                                          'extracting')
        ) AS awaiting_extraction,
        min(e.received_datetime) AS oldest_received,
        max(e.received_datetime) AS newest_received,
        max(e.updated_at) AS last_activity
    FROM mailboxes m
    LEFT JOIN email_messages e ON e.mailbox_id = m.id
    WHERE m.user_id = :user_id
    HAVING count(m.id) > 0
    """
)

# status_rank back to the name the API speaks.
_STATUS_BY_RANK = {1: "needs_reauth", 2: "disconnected", 3: "active"}

_UPSERT_MAILBOX = text(
    """
    INSERT INTO mailboxes
        (id, tenant_id, user_id, ms_user_id, scope, folder_id,
         initial_sync_from, retention_months)
    VALUES (:id, :tenant_id, :user_id, :ms_user_id, 'whole_inbox', 'inbox',
            :initial_sync_from, :retention_months)
    ON CONFLICT (tenant_id, ms_user_id, folder_id) DO UPDATE SET
        status = 'active',
        user_id = EXCLUDED.user_id
    RETURNING id
    """
)


# Bring back every mailbox this user owns that the dead grant took down — not
# one. The unique constraint is per folder, so a user can hold several, and the
# grant that broke was shared by all of them. Reviving one and calling it done
# is the same silence `_MAILBOX_STATE` exists to break: a dashboard reporting
# active while a second mailbox stayed dark.
#
# `initial_sync_from` is untouched:
# the user chose that window once, and re-walking it would re-read history they
# did not ask for a second time. Returns nothing when the row was already
# active, so a re-consent by someone whose mailbox is fine does not churn a
# healthy subscription.
_REVIVE_MAILBOX = text(
    """
    UPDATE mailboxes SET status = 'active'
    WHERE user_id = :user_id AND status IN ('needs_reauth', 'disconnected')
    RETURNING id
    """
)


async def _provision_mailbox(
    session,
    tenant_uuid: uuid.UUID,
    user_id: uuid.UUID,
    ms_object_id: str,
    initial_sync_from: datetime,
) -> uuid.UUID:
    """Create — or reactivate — a mailbox the user has asked us to read.

    `initial_sync_from` is the user's answer to "how far back?", passed in
    rather than defaulted here. Consent alone no longer provisions anything:
    approving a permission and choosing to import three months of mail are two
    different decisions, and the second one is theirs to make (§6.2).

    Reusing the row on conflict is what makes reconnection work: mail already
    ingested stays attached to it, and flipping `status` back to active is
    exactly what a mailbox coming out of `needs_reauth` needs.
    `initial_sync_from` is deliberately NOT overwritten on conflict — a
    reconnection should not silently re-walk history again.
    """
    return (
        await session.execute(
            _UPSERT_MAILBOX,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_uuid,
                "user_id": user_id,
                "ms_user_id": ms_object_id,
                "initial_sync_from": initial_sync_from,
                "retention_months": settings.DEFAULT_RETENTION_MONTHS,
            },
        )
    ).scalar_one()


async def _store_mailbox_consent(
    request: Request,
    envelope: dict,
    result: dict,
    tenant_uuid: uuid.UUID,
    oid: str,
    tid: str,
) -> RedirectResponse:
    """Record a mailbox consent against the user who asked for it.

    Deliberately does *not* touch the session cookie. This flow is an existing
    user granting an extra permission, not an authentication: re-issuing the
    session here would turn "connect my mailbox" into a silent sign-in, and any
    account that completed the round trip could take over the session.

    Which is the trap this guards. Microsoft will happily let the user sign in
    to the consent page as somebody else — a second work account, a personal
    one — and hand us a perfectly valid token for *that* mailbox. Storing it
    against the session user would file one person's mail credential under
    another person's row, across tenants. So the returned identity must be the
    session's own, checked before anything is written.
    """
    stored_session = envelope.get("session") or {}
    try:
        user_id = uuid.UUID(stored_session["uid"])
        session_tenant = uuid.UUID(stored_session["tid"])
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("ms_mailbox_consent_rejected", reason="flow_missing_session")
        raise HTTPException(status_code=400, detail="Sign-in state is invalid or expired.") from exc

    if tenant_uuid != session_tenant:
        log.warning("ms_mailbox_consent_rejected", reason="tenant_mismatch")
        raise HTTPException(
            status_code=400,
            detail="That Microsoft account belongs to a different organisation than the one "
            "you are signed in to. Sign in as that account first, then connect its mailbox.",
        )

    now = datetime.now(UTC)
    async with tenant_session(tenant_uuid) as session:
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=401, detail="Not signed in.")
        # The immutable object id, not the mail address: an address can be
        # reassigned, and this check is the whole guard.
        if user.ms_object_id != oid:
            log.warning("ms_mailbox_consent_rejected", reason="account_mismatch")
            raise HTTPException(
                status_code=400,
                detail="You approved that with a different Microsoft account than the one you "
                "are signed in with. Connect the mailbox of the account you signed in as.",
            )

        # The Entra tenant GUID, not our tenant primary key: for a personal
        # account those differ, and this is MSAL's account key, not ours.
        # Authoritative: this flow asked for every scope there is, so what came
        # back is the whole current grant — including when the user declined
        # part of it. Storing it unconditionally is also the only way a stale
        # refresh token can ever be rotated.
        await _store_token_if_not_a_downgrade(
            session, result, tenant_uuid, user_id, f"{oid}.{tid}", now, authoritative=True
        )

        # Nothing is *provisioned* here, on purpose. Consent grants permission
        # to read the mailbox; it does not say which of it to read. A first
        # connection therefore ends with a token and nothing else, and the
        # period is chosen at `POST /mailbox/ingest` (§6.2).
        #
        # A reconnection is the other case, and it must not be confused with
        # the first. The period was already chosen; what broke was the grant.
        # So an existing mailbox is revived here rather than being sent back
        # through the chooser — which it could not pass anyway, since
        # `/mailbox/ingest` refuses a mailbox that already exists. Without this
        # a `needs_reauth` mailbox stayed stuck forever: re-consenting stored a
        # working token beside a row nothing would look at again.
        revived = (
            list(
                (await session.execute(_REVIVE_MAILBOX, {"user_id": user_id})).scalars().all()
            )
            if _covers_mailbox(result.get("scope"))
            else []
        )

    if revived:
        # The session block above has committed, so a dashboard refetching on
        # this nudge sees the mailbox active. Published once for the whole
        # revival rather than per mailbox: a user holding three of them has one
        # grant, fixed once, and three nudges would be three refetches of the
        # same screen.
        await publish(tenant_uuid, KIND_MAILBOX)

    for mailbox_id in revived:
        # The subscription died with the grant, so recreating it is the whole
        # of resuming. The backfill is deliberately not re-run: its window was
        # settled at connect time and history has not moved.
        await enqueue(
            "recreate_subscription", tenant_id=str(tenant_uuid), mailbox_id=str(mailbox_id)
        )

    log.info(
        "ms_mailbox_consent_stored",
        tenant_id=str(tenant_uuid),
        user_id=str(user_id),
        granted=_covers_mailbox(result.get("scope")),
        revived=len(revived),
    )

    response = RedirectResponse(_frontend_url(DASHBOARD_PATH))
    for name in request.cookies:
        if name.startswith(FLOW_COOKIE_PREFIX):
            response.delete_cookie(name, path="/")
    return response


# The widest value `users.preferred_name` can physically hold, derived from
# the column rather than written out — same reasoning as `MAX_EXPECTED_SALARY`
# in app/api/candidates.py, so a migration that resizes the column moves this
# bound with it instead of leaving a stale literal behind.
MAX_PREFERRED_NAME_LENGTH = User.__table__.c.preferred_name.type.length

# This string is interpolated straight into a WhatsApp draft (see
# `_whatsapp_draft_text` in app/api/candidates.py). A newline or other control
# character would let a "name" forge extra lines in a message the candidate
# reads as coming from the agency, so it is refused outright rather than
# stripped — stripping would silently change what the user asked to be called.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class PreferredNameIn(BaseModel):
    # `str | None` so an explicit `{"preferred_name": null}` is distinguishable
    # from omitting the field entirely once decoded — FastAPI/Pydantic gives a
    # required field no way to be "absent", which is fine here: this endpoint
    # has exactly one field, and PATCH-ing it with nothing to say is not a
    # request this route needs to support.
    preferred_name: str | None


@router.patch("/auth/me")
async def update_me(request: Request, body: PreferredNameIn) -> dict[str, dict]:
    """Set or clear the caller's own display name. No admin path, no user id
    in the URL — `_require_session` is the only source of whose row this
    touches, so there is nothing here that could target anyone else's.

    Returns the same shape as `GET /auth/me` so the frontend can replace its
    cached `me` wholesale rather than patching two representations by hand.
    """
    user_uuid, tenant_uuid = _require_session(request)

    if body.preferred_name is None:
        # Explicit null clears the choice and returns the user to whatever
        # the provider's `display_name` says — that is the escape hatch, not
        # a bug: there is no other way back to "unset" once a name is typed.
        cleaned: str | None = None
    else:
        stripped = body.preferred_name.strip()
        if _CONTROL_CHARACTERS.search(stripped):
            raise HTTPException(
                status_code=422,
                detail="preferred_name must not contain newlines or control characters",
            )
        if len(stripped) > MAX_PREFERRED_NAME_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=f"preferred_name must be at most {MAX_PREFERRED_NAME_LENGTH} characters",
            )
        # A blank or whitespace-only string stores NULL, not "" — "" would be
        # indistinguishable from "unset" to nothing, and the resolution chain
        # in app/services/user_naming.py needs to tell "no choice made" apart
        # from "chose an empty name" (which is not a real choice at all).
        cleaned = stripped or None

    async with tenant_session(tenant_uuid) as session:
        result = await session.execute(
            update(User).where(User.id == user_uuid).values(preferred_name=cleaned)
        )
        if result.rowcount == 0:
            # A deleted user with a live cookie must not look signed in.
            raise HTTPException(status_code=401, detail="Not signed in.")
        await session.commit()

    return await me(request)


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
    user_uuid, tenant_uuid = _require_session(request)

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

        # Only the scope is read, never the ciphertext: decrypting would put a
        # live refresh token in memory to answer a question the scope already
        # answers, and a token Entra has since revoked still decrypts fine — so
        # decryption would not even make the answer more truthful.
        #
        # The row's existence is NOT the signal, though it used to be. Sign-in
        # asks only for identity scopes and `offline_access` still yields a
        # refresh token, so every signed-in user now has a row. Reading that as
        # "mailbox connected" would tell someone their mail was hooked up when
        # nothing had been granted to read it. What counts is the grant.
        scope = (
            await session.execute(
                select(MicrosoftToken.scope).where(MicrosoftToken.user_id == user.id)
            )
        ).scalar_one_or_none()
        connected = _covers_mailbox(scope)

        # A granted scope is a permission; ingestion is a mailbox with a live
        # Graph subscription behind it. They come apart in both directions — a
        # consent whose subscription creation failed, a mailbox flipped to
        # `needs_reauth` while the grant is still on file — so this is read,
        # never inferred.
        state = (
            await session.execute(_MAILBOX_STATE, {"user_id": user.id})
        ).one_or_none()

    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "preferred_name": user.preferred_name,
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
            # The grant is in place but the user has not yet said how far back
            # to read. This is a state of its own, not a half-broken connection:
            # the dashboard owes them the preview and the chooser, not a
            # spinner suggesting work is happening (§6.2).
            "awaiting_period": connected and state is None,
            "scopes": (scope or "").split(),
            # None until a mailbox row exists — signing in alone provisions
            # nothing. `needs_reauth` is the one the UI must not swallow: the
            # grant is still on file, so `connected` stays true while nothing
            # is being read.
            "status": _STATUS_BY_RANK.get(state.status_rank) if state else None,
            # At least one active mailbox with a live subscription behind it. A
            # connected mailbox is not the same as one being ingested.
            "ingestion_active": bool(state and state.subscribed),
            "ingested": {
                "total": state.total if state else 0,
                # Being fetched right now.
                "in_progress": state.in_progress if state else 0,
                # Fetched and stored, with nothing yet to read them.
                "awaiting_extraction": state.awaiting_extraction if state else 0,
                # Emails that finished extraction — how much mail was read.
                "emails_extracted": state.emails_extracted if state else 0,
                # Vacancies found in them — what the table lists. One email can
                # advertise six roles, so these two are different numbers and
                # naming them the same thing is how the page came to show "3
                # job orders found" above a table of 7.
                "opportunities": state.opportunities if state else 0,
            },
            "oldest_received": _iso(state.oldest_received if state else None),
            "newest_received": _iso(state.newest_received if state else None),
            "last_activity": _iso(state.last_activity if state else None),
        },
    }
