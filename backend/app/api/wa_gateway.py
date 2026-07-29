"""WA gateway routes (spec 2026-07-29-baileys-gateway-plan.md, §4-§6).

Naming firewall, restated because it is easy to get wrong by pattern-matching
on the existing `whatsapp_webhook.py`: this file is `wa_gateway.py`, its
routes live under `/api/wa/…`, and nothing here shares a name with the
existing Meta Cloud API notification channel (`WHATSAPP_*`, `whatsapp_*`
tables). That channel sends templated notifications *to* recruiters; this one
is a recruiter's own WhatsApp number, paired via Baileys, sending *as* them.

**Two different trust directions share this module:**

- `POST/GET /session*` are browser-facing, behind `_require_session` — the
  three routes below. A session belongs to exactly one `(tenant_id, user_id)`,
  the signed-in caller's own, never an id accepted from the request body or
  query string (§18): the whole point of scoping by the cookie is that agency
  B cannot reach agency A's WhatsApp session by guessing or forging an id.
- `POST /internal/status` is gateway-facing, authenticated by the shared
  secret exactly like `telegram_webhook.py` authenticates Telegram, and is
  the *only* thing in this codebase allowed to write `wa_sessions.status`
  (plan §6: "Writer: the gateway only, always via the FastAPI internal
  callback, so FastAPI is the single DB writer and the SSE publisher").

**Why the browser routes don't read `wa_sessions` at all.** The QR is never
persisted (plan §5), so a GET that answered from the database could never
show a live QR — it would have to call the gateway anyway for that one field.
Rather than split "some fields from Postgres, one field from the gateway,
hope they agree", every browser-facing route asks the gateway once and
returns exactly what it said. `wa_sessions` still gets written (via the
internal callback below) purely so the status survives this process
restarting and so `wa_session` SSE nudges have something to point the
refetch at — the row is not this module's source of truth for a request in
flight.
"""

import secrets as secrets_module
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.api.auth import _require_session
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.wa_session import STATUS_DISCONNECTED
from app.services import events as events_service
from app.services.wa_gateway import GatewayUnreachableError, WaGatewayClient
from app.services.wa_risk_notice import NOTICE_TEXT, NOTICE_VERSION

log = get_logger(__name__)
router = APIRouter(prefix="/wa", tags=["wa_gateway"])

# API-only status, never written to `wa_sessions` — the CHECK constraint in
# `20260729_2100_wa_gateway_sessions.py` would reject it, and that is
# deliberate: "the gateway cannot be reached" is a fact about this request,
# not a fact about the recruiter's WhatsApp connection, and persisting it
# would tell the next request (or the next recruiter's browser) a stale lie
# about a session that may be perfectly `connected`.
STATUS_GATEWAY_UNREACHABLE = "gateway_unreachable"


def _client() -> WaGatewayClient:
    return WaGatewayClient()


def _unreachable_body(*, include_details: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"status": STATUS_GATEWAY_UNREACHABLE, "qr": None, "expires_at": None}
    if include_details:
        body |= {"phone_number": None, "connected_at": None, "last_checked_at": None}
    return body


async def _call_gateway(
    action: str, tenant_id: uuid.UUID, user_id: uuid.UUID, *, include_details: bool
) -> dict[str, Any]:
    """Shared unreachable-gateway handling for the three browser routes.

    `action` is one of the `WaGatewayClient` method names below.
    `include_details` is True only for `GET /session`, whose contract carries
    three fields (`phone_number`, `connected_at`, `last_checked_at`) that
    `POST /session` and `POST /session/disconnect` do not.
    """
    if not settings.wa_gateway_configured():
        # Same shape as `graph_configured()`: an unset URL/secret is a real
        # deployment state (a new `gateway` service starts with zero env
        # vars — CLAUDE.md's GRAPH_BASE_URL/R2_* precedent), answered here
        # rather than surfacing as an httpx error with no host.
        return _unreachable_body(include_details=include_details)
    client = _client()
    method = getattr(client, action)
    try:
        snapshot = await method(str(tenant_id), str(user_id))
    except GatewayUnreachableError:
        return _unreachable_body(include_details=include_details)

    body: dict[str, Any] = {
        "status": snapshot.status,
        "qr": snapshot.qr,
        "expires_at": snapshot.expires_at,
    }
    if include_details:
        # "when we last looked", not "when WhatsApp last saw the user" (§15)
        # — this request *is* that look, so `now()` is the honest value
        # rather than a database column nothing here has read.
        body |= {
            "phone_number": snapshot.phone_number,
            "connected_at": snapshot.connected_at,
            "last_checked_at": datetime.now(tz=UTC).isoformat(),
        }
    return body


async def _load_risk_ack(
    tenant_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[datetime | None, str | None]:
    """This user's own `(risk_acknowledged_at, risk_notice_version)`.

    Reads `wa_sessions` directly (unlike the gateway-backed fields on this
    module's other routes) because an acknowledgement is a fact FastAPI owns
    outright — the gateway has no opinion on it. `tenant_session` scopes the
    query to the caller's own tenant (§18); the row may not exist yet for a
    recruiter who has never started pairing, which reads as "not
    acknowledged", not an error.
    """
    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT ban_risk_acknowledged_at, risk_notice_version "
                    "FROM wa_sessions WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )
        ).first()
    if row is None:
        return None, None
    return row[0], row[1]


def _is_current_ack(ack_at: datetime | None, ack_version: str | None) -> bool:
    """An acknowledgement only counts if it is of *this* notice text.

    A recruiter who acknowledged an older wording has not acknowledged the
    risk as currently described — the version must match `NOTICE_VERSION`
    exactly, not merely be non-null.
    """
    return ack_at is not None and ack_version == NOTICE_VERSION


@router.post("/session")
async def start_session(request: Request) -> dict[str, Any]:
    """Start or resume pairing.

    Idempotent by construction of the gateway's `/sessions/pair`: called
    while already `connected` it returns the current state without touching
    the live socket, and called while `pairing` it returns the *existing* QR
    rather than starting a second one — two browser tabs open on the same
    settings page see the same code.

    Refuses without a *current* risk acknowledgement (plan §9). This is a
    409, not a new `session_status` — the CHECK constraint on `wa_sessions`
    names exactly five statuses and none of them is this; the reason lives in
    a separate `reason` key instead.
    """
    user_id, tenant_id = _require_session(request)
    ack_at, ack_version = await _load_risk_ack(tenant_id, user_id)
    if not _is_current_ack(ack_at, ack_version):
        # A flat body, not `HTTPException(detail={...})` — that wraps
        # whatever is passed inside another `{"detail": ...}`, and the
        # contract here is exactly two top-level keys. Deliberately not a
        # new `session_status`: the CHECK constraint on `wa_sessions` names
        # five and only five, and an earlier phase of this build shipped a
        # contract that violated exactly that by inventing a sixth.
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    "You must acknowledge the WhatsApp ban-risk notice "
                    "before pairing."
                ),
                "reason": "risk_not_acknowledged",
            },
        )
    return await _call_gateway("pair", tenant_id, user_id, include_details=False)


@router.get("/session")
async def get_session(request: Request) -> dict[str, Any]:
    """Current status. This is what an SSE `wa_session` nudge refetches.

    Carries both what this user has acknowledged (`risk_acknowledged_at`,
    `risk_notice_version` — null only if never acknowledged; otherwise the
    exact version recorded, unchanged even if the notice text has since
    moved on) and what is current (`notice_version`, `notice_text`). The
    caller compares the two versions itself to tell "never acknowledged"
    apart from "acknowledged a version that is no longer current" — this
    route does not make that judgement or null the field out on its behalf.
    """
    user_id, tenant_id = _require_session(request)
    body = await _call_gateway("status", tenant_id, user_id, include_details=True)
    ack_at, ack_version = await _load_risk_ack(tenant_id, user_id)
    body |= {
        "risk_acknowledged_at": ack_at,
        "risk_notice_version": ack_version,
        "notice_version": NOTICE_VERSION,
        "notice_text": NOTICE_TEXT,
    }
    return body


class ConsentIn(BaseModel):
    """Body of `POST /api/wa/consent` — the version the recruiter was shown."""

    notice_version: str


@router.post("/consent")
async def record_consent(request: Request, body: ConsentIn) -> dict[str, Any]:
    """Record acknowledgement of the ban-risk notice (plan §9).

    422, not 409, for a stale or unknown version: acknowledging text the
    recruiter was not actually shown is not consent, and the caller sent a
    bad request rather than hitting a conflict with server state. §18: this
    always writes the signed-in caller's own `(tenant_id, user_id)` row,
    never an id taken from the request.
    """
    user_id, tenant_id = _require_session(request)
    if body.notice_version != NOTICE_VERSION:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{body.notice_version}' is not the current notice version "
                f"('{NOTICE_VERSION}'). Reload and acknowledge the current "
                "notice."
            ),
        )
    async with tenant_session(tenant_id) as session:
        # `status` is spelled out on insert rather than left to the column's
        # `pairing` default: acknowledging the notice does not start a
        # pairing attempt, and a row claiming `pairing` when nothing is
        # pairing is a false state both a human reader and the stale-pending
        # sweep could act on. `disconnected` — no live session — is the
        # honest word for "acknowledged, nothing paired yet". An existing
        # row's `status` is never touched here; only the gateway's internal
        # callback (`internal_status` below) owns that column once a session
        # exists.
        await session.execute(
            text(
                """
                INSERT INTO wa_sessions
                    (id, tenant_id, user_id, status, ban_risk_acknowledged_at, risk_notice_version)
                VALUES
                    (:user_id, :tenant_id, :user_id, :status, now(), :notice_version)
                ON CONFLICT (user_id) DO UPDATE SET
                    ban_risk_acknowledged_at = now(),
                    risk_notice_version = EXCLUDED.risk_notice_version,
                    updated_at = now()
                """
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "status": STATUS_DISCONNECTED,
                "notice_version": body.notice_version,
            },
        )
    ack_at, ack_version = await _load_risk_ack(tenant_id, user_id)
    return {
        "risk_acknowledged_at": ack_at,
        "risk_notice_version": ack_version,
        "notice_version": NOTICE_VERSION,
        "notice_text": NOTICE_TEXT,
    }


@router.post("/session/disconnect")
async def disconnect_session(request: Request) -> dict[str, str]:
    """A recruiter's own choice to disconnect.

    Deliberately distinct from `logged_out` (plan §6, restated by review):
    WhatsApp forcing the link closed means re-pairing, and repeated
    re-pairing is itself a ban signal (plan §11) — so this path must never be
    confused with that one. The gateway logs the socket out and clears its
    stored auth state; the fixed response below matches this route's
    contract regardless of the gateway's own vocabulary, because from the
    browser's perspective there is exactly one outcome worth naming.
    """
    user_id, tenant_id = _require_session(request)
    if settings.wa_gateway_configured():
        try:
            await _client().disconnect(str(tenant_id), str(user_id))
        except GatewayUnreachableError:
            # A disconnect the gateway never received is not "still
            # connected" from the recruiter's point of view — they asked to
            # stop, and the honest local answer is "disconnected" even if the
            # gateway-side state is unresolved until it comes back and a
            # liveness check corrects it (plan §6). Never a 500 for a dead
            # gateway (CLAUDE.md).
            log.warning("wa_disconnect_gateway_unreachable", tenant_id=str(tenant_id))
    return {"status": "disconnected"}


class InternalStatusIn(BaseModel):
    """Body the gateway posts on every status/QR change (plan §5, §6).

    No `qr` field — the QR is never persisted (plan §5); this callback exists
    to durably record status, phone number and connection time, and to
    trigger the SSE nudge. A caller that wants the live QR calls
    `GET /api/wa/session`, which asks the gateway directly.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    # The five the CHECK constraint allows, stated here so an unknown value is
    # a 422 naming the field rather than an IntegrityError surfacing as a 500.
    # `gateway_unreachable` is deliberately absent: it is this API's way of
    # saying it could not see, never something the gateway observed, and a row
    # claiming it would be a fact we never had.
    status: Literal["pairing", "connected", "reconnecting", "disconnected", "logged_out"]
    status_detail: str | None = None
    phone_e164: str | None = None
    connected_at: datetime | None = None
    qr_expires_at: datetime | None = None


async def apply_internal_status(body: InternalStatusIn) -> None:
    """The single place `wa_sessions.status` is written (plan §6).

    Split out of the `POST /internal/status` route so the liveness sweep
    (`app.workers.tasks.sweep_wa_liveness`) can call the exact same write —
    not a second implementation of it — when it re-delivers a status the
    gateway already told us but that never landed, because
    `gateway/src/callback.ts` is fire-and-forget and a lost push otherwise
    stays lost forever (asking `status()` again produces no new *change*
    event, so no retry is ever generated on the gateway side). The sweep
    calls this function directly, in-process, rather than over HTTP: this
    codebase has no precedent for the worker calling the api service back
    over the network (every other sweep talks to Postgres directly), the two
    processes already share one database, and a real HTTP hop would need a
    self-referencing URL nothing here currently configures — trading one
    unreachable-gateway case for a second, symmetrical
    unreachable-ourselves case that buys nothing. Calling this function *is*
    "the same writer the gateway uses": it is the identical code, run
    in-process instead of round-tripped, so there is still exactly one
    place that turns a gateway-reported status into a database write.

    The HTTP route above still owns authentication; a caller of this
    function is already trusted (either the gateway, past the shared-secret
    check, or our own worker process) — this function itself performs no
    auth check, by design, since it is not the network boundary.
    """
    async with tenant_session(body.tenant_id) as session:
        # `id = user_id`: the gateway's addressing convention (see
        # gateway/src/sessions.ts, "Session identity") — `ensureWaSessionRow`
        # on that side inserts the same skeleton with the same identity
        # before it ever writes a key, so this UPSERT lands on that same row
        # rather than racing it into creating a second one.
        await session.execute(
            text(
                """
                INSERT INTO wa_sessions
                    (id, tenant_id, user_id, status, status_detail, phone_e164,
                     last_connected_at, qr_expires_at)
                VALUES
                    (:user_id, :tenant_id, :user_id, :status, :status_detail,
                     :phone_e164, :connected_at, :qr_expires_at)
                ON CONFLICT (user_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    status_detail = EXCLUDED.status_detail,
                    phone_e164 = COALESCE(EXCLUDED.phone_e164, wa_sessions.phone_e164),
                    last_connected_at = COALESCE(
                        EXCLUDED.last_connected_at, wa_sessions.last_connected_at
                    ),
                    qr_expires_at = EXCLUDED.qr_expires_at,
                    updated_at = now()
                """
            ),
            {
                "tenant_id": body.tenant_id,
                "user_id": body.user_id,
                "status": body.status,
                "status_detail": body.status_detail,
                "phone_e164": body.phone_e164,
                "connected_at": body.connected_at,
                "qr_expires_at": body.qr_expires_at,
            },
        )

    # After commit (the `async with` block above has already committed), per
    # `events_service.publish`'s own docstring: a nudge that overtakes its own
    # row makes the browser refetch and see nothing.
    await events_service.publish(body.tenant_id, events_service.KIND_WA_SESSION)


@router.post("/internal/status")
async def internal_status(
    body: InternalStatusIn,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    """The gateway's push callback — the only *network* writer of
    `wa_sessions.status`. `apply_internal_status` above is the only writer,
    full stop; this route is one of its two trusted callers.

    Authenticated the same way `telegram_webhook.py` authenticates Telegram:
    a shared secret compared with `secrets.compare_digest`, because a plain
    `==` leaks its answer through timing, and an empty configured secret
    always rejects (an unset secret must never mean "accept anything").
    """
    expected = settings.WA_GATEWAY_SHARED_SECRET
    presented = _bearer(authorization)
    if not expected or not presented or not secrets_module.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Unauthorised.")

    await apply_internal_status(body)
    return {"status": "ok"}


def _bearer(header: str | None) -> str:
    if not header:
        return ""
    prefix = "Bearer "
    return header[len(prefix) :] if header.startswith(prefix) else ""
