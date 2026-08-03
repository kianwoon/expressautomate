"""Choosing what gets sent where (spec 2026-07-28).

Every endpoint reads the tenant from the session cookie and works inside
`tenant_session`, so a destination id belonging to another agency simply is not
found — the policy answers before any code here has to.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from app.api.auth import _require_session
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_TELEGRAM,
    CHANNEL_WHATSAPP,
    CHANNEL_WHATSAPP_LINKED,
)
from app.models.wa_session import STATUS_CONNECTED as WA_STATUS_CONNECTED
from app.services.notify.channels import channel_for
from app.services.notify.channels.base import SendOutcome
from app.services.notify.events import ALL_EVENT_KINDS
from app.services.notify.linking import (
    create_destination,
    issue_token,
    opt_in_attempts_this_hour,
    record_verify_attempt,
    redeem_token,
)
from app.services.notify.render import WhatsAppContent

log = get_logger(__name__)

router = APIRouter(tags=["notifications"])

# allow-hardcode: SQL statements, not a phrase list.

# The ownership rule, in one fragment reused by every statement in this module
# that resolves a destination for the caller.
#
# RLS narrows a destination to the agency; this narrows a *paired device* to
# the one recruiter who paired it. Every other channel is an account the agency
# holds — a Telegram chat or the shared WABA number — so a colleague seeing it,
# ticking events onto it, or unlinking it is ordinary shared-settings work. A
# `whatsapp_linked` destination is somebody's personal handset, reached over a
# Baileys socket that only their own session can open: it is not an agency feed
# and there is no agency-wide form of it. Left tenant-scoped it went wrong in
# both directions at once — a colleague could subscribe their own job orders to
# your phone, and the settings screen, seeing a linked destination already
# present, hid the pairing panel so they could never add their own device.
# Outside its owner the row therefore reads as absent, which is the same answer
# RLS gives across tenants and the same 404 this module already returns for it.
_OWN_DEVICE_ONLY = "(d.channel <> :linked_channel OR d.user_id = :user_id)"

_LIST_DESTINATIONS = text(
    f"""
    SELECT d.id, d.channel, d.user_id, d.verified_at, d.disabled_at,
           coalesce(
               array_agg(s.event_kind) FILTER (WHERE s.active), ARRAY[]::text[]
           ) AS event_kinds
    FROM notification_destinations d
    LEFT JOIN notification_subscriptions s ON s.destination_id = d.id
    WHERE {_OWN_DEVICE_ONLY}
    GROUP BY d.id
    ORDER BY d.created_at
    """
)

_DESTINATION_EXISTS = text(
    f"SELECT d.id FROM notification_destinations d "
    f"WHERE d.id = :id AND {_OWN_DEVICE_ONLY}"
)

_CLEAR_SUBSCRIPTIONS = text(
    "DELETE FROM notification_subscriptions WHERE destination_id = :destination_id"
)

_ADD_SUBSCRIPTION = text(
    """
    INSERT INTO notification_subscriptions
        (id, tenant_id, destination_id, event_kind, active)
    VALUES (:id, :tenant_id, :destination_id, :event_kind, true)
    """
)

_DELETE_DESTINATION = text(
    f"DELETE FROM notification_destinations AS d "
    f"WHERE d.id = :id AND {_OWN_DEVICE_ONLY} RETURNING d.id"
)

# The caller's own paired device. `wa_sessions.user_id` is globally unique
# (one device, one recruiter), so this is at most one row.
_MY_WA_SESSION = text(
    """
    SELECT status, phone_e164 FROM wa_sessions WHERE user_id = :user_id
    """
)

# Scoped the same way, and the order matters: a colleague's paired device must
# read as absent *before* the channel check below can answer 400 and tell them
# it exists.
_DESTINATION_CHANNEL = text(
    f"SELECT d.channel FROM notification_destinations d "
    f"WHERE d.id = :id AND {_OWN_DEVICE_ONLY}"
)

_SET_SCOPE = text(
    f"""
    UPDATE notification_destinations AS d
    SET user_id = :new_user_id
    WHERE d.id = :id AND {_OWN_DEVICE_ONLY}
    RETURNING d.id
    """
)


def _mine(user_id: uuid.UUID) -> dict:
    """The binds `_OWN_DEVICE_ONLY` needs. One helper so a statement cannot be
    executed with the predicate compiled in and the caller left out."""
    return {"user_id": user_id, "linked_channel": CHANNEL_WHATSAPP_LINKED}


class SubscriptionUpdate(BaseModel):
    destination_id: uuid.UUID
    event_kinds: list[str] = Field(default_factory=list)

    @field_validator("event_kinds")
    @classmethod
    def known_events_only(cls, value: list[str]) -> list[str]:
        """422 rather than a stored typo.

        An unknown kind would be accepted, displayed, and never fired — a
        subscription that looks active and is not.
        """
        unknown = sorted(set(value) - set(ALL_EVENT_KINDS))
        if unknown:
            raise ValueError(f"Unknown event kinds: {', '.join(unknown)}")
        return value


class OptInRequest(BaseModel):
    # E.164. Meta rejects anything else, and the rejection arrives as a failed
    # send with no obvious cause.
    phone_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")


class VerifyRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class ScopeUpdate(BaseModel):
    # "user" means only the person who linked it; "tenant" means the agency's
    # shared feed, which everyone's job orders reach.
    scope: Literal["user", "tenant"]


@router.get("/notifications/settings")
async def notification_settings(request: Request) -> dict:
    """Everything the settings screen needs in one read."""
    user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        rows = (await session.execute(_LIST_DESTINATIONS, _mine(user_id))).all()
        wa = (
            await session.execute(_MY_WA_SESSION, {"user_id": user_id})
        ).one_or_none()

    # Availability here is per-caller, not per-deployment, unlike the other two
    # channels: what makes this one usable is *this* recruiter having a device
    # paired right now, and only a connected session can send.
    wa_connected = wa is not None and wa.status == WA_STATUS_CONNECTED

    return {
        "events": [{"kind": kind} for kind in ALL_EVENT_KINDS],
        "channels": {
            CHANNEL_TELEGRAM: settings.telegram_configured(),
            CHANNEL_WHATSAPP: settings.whatsapp_configured(),
            CHANNEL_WHATSAPP_LINKED: wa_connected,
        },
        # Which device, so the screen can name the number rather than saying
        # "your WhatsApp" and leaving the recruiter to guess.
        "whatsapp_linked_number": wa.phone_e164 if wa_connected else None,
        "destinations": [
            {
                "id": str(row.id),
                "channel": row.channel,
                # Null means the agency's shared feed rather than one person's.
                "scope": "tenant" if row.user_id is None else "user",
                # `scope == "user"` only says the row is *somebody's* personal
                # destination, not the caller's — a colleague's own paired
                # WhatsApp or linked Telegram chat is `scope: "user"` too. The
                # setup-nudge checklist (`frontend/app/setup-tasks.ts`) needs
                # to tell "I personally have alerts on" apart from "someone in
                # my agency does", so it is spelled out here rather than
                # derived from `scope` a second time on the frontend.
                "mine": row.user_id == user_id,
                "verified": row.verified_at is not None,
                "disabled": row.disabled_at is not None,
                "event_kinds": list(row.event_kinds),
            }
            for row in rows
        ],
    }


@router.put("/notifications/subscriptions")
async def set_subscriptions(request: Request, payload: SubscriptionUpdate) -> dict:
    """Replace this destination's subscriptions with exactly what was sent.

    Replace rather than merge: the screen sends the full set of ticked boxes,
    and a merge would make unticking one impossible.
    """
    user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        exists = (
            await session.execute(
                _DESTINATION_EXISTS,
                {"id": payload.destination_id, **_mine(user_id)},
            )
        ).one_or_none()
        if exists is None:
            # Under RLS another tenant's destination reads as absent, which is
            # the honest answer: it does not exist for this caller. A
            # colleague's paired device is absent for the same reason — see
            # `_OWN_DEVICE_ONLY`.
            raise HTTPException(status_code=404, detail="Destination not found.")

        await session.execute(
            _CLEAR_SUBSCRIPTIONS, {"destination_id": payload.destination_id}
        )
        for kind in payload.event_kinds:
            await session.execute(
                _ADD_SUBSCRIPTION,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "destination_id": payload.destination_id,
                    "event_kind": kind,
                },
            )

    return {"status": "updated", "event_kinds": payload.event_kinds}


@router.post("/notifications/destinations/telegram/link")
async def telegram_link(request: Request) -> dict:
    """A one-time deep link. Pressing it is what proves the account is theirs."""
    user_id, tenant_id = _require_session(request)
    if not settings.telegram_configured():
        raise HTTPException(status_code=503, detail="Telegram is not configured.")

    async with tenant_session(tenant_id) as session:
        token = await issue_token(session, tenant_id, user_id, CHANNEL_TELEGRAM)

    return {
        "url": f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={token}",
        "expires_in_minutes": settings.NOTIFY_LINK_TOKEN_TTL_MINUTES,
    }


@router.post("/notifications/destinations/whatsapp/opt-in")
async def whatsapp_opt_in(request: Request, payload: OptInRequest) -> dict:
    """Send a code to a number the user typed.

    Rate limited because it is otherwise an OTP pump: anyone signed in could
    have us message arbitrary numbers, and the reputation being spent belongs
    to a WABA every tenant shares.
    """
    user_id, tenant_id = _require_session(request)
    if not settings.whatsapp_configured():
        raise HTTPException(status_code=503, detail="WhatsApp is not configured.")

    async with tenant_session(tenant_id) as session:
        attempts = await opt_in_attempts_this_hour(session, user_id, CHANNEL_WHATSAPP)
        if attempts >= settings.NOTIFY_OPT_IN_MAX_PER_HOUR:
            raise HTTPException(
                status_code=429,
                detail="Too many verification codes requested. Try again in an hour.",
            )
        code = await issue_token(
            session, tenant_id, user_id, CHANNEL_WHATSAPP, address=payload.phone_number
        )

    result = await channel_for(CHANNEL_WHATSAPP).send(
        payload.phone_number,
        WhatsAppContent(
            template_name=settings.WHATSAPP_TEMPLATE_LINK_CODE,
            language=settings.WHATSAPP_TEMPLATE_LANG,
            body_params=[code],
            # Meta's authentication templates carry the code on the button too,
            # which is what makes the one-tap copy work in the app.
            button_param=code,
        ),
    )
    if result.outcome is not SendOutcome.SENT:
        log.warning("whatsapp_opt_in_failed", error=result.error)
        raise HTTPException(
            status_code=502, detail="Could not send the verification code."
        )

    return {"status": "sent", "expires_in_minutes": settings.NOTIFY_LINK_TOKEN_TTL_MINUTES}


@router.post("/notifications/destinations/whatsapp/verify")
async def whatsapp_verify(request: Request, payload: VerifyRequest) -> dict:
    """Redeem the code, and only then record the number as a destination.

    Rate limited the same way the opt-in send is, but for the opposite abuse:
    the code is six digits, so a live one is guessable inside its TTL. RLS
    keeps a guess from reaching another tenant's code (`redeem_token` runs
    inside this request's `tenant_session`), but it does nothing for a
    same-tenant actor trying every value against a colleague's code — that is
    bounded here, per user, before the guess is even checked against the
    database.
    """
    user_id, tenant_id = _require_session(request)
    # Increment first, then check: the count returned is this attempt's own
    # ordinal, so the (MAX+1)th caller is the one that gets refused, and two
    # concurrent requests can't both read a stale count and both slip through
    # (Redis' INCR is atomic; two overlapping reads of a Python dict was the
    # bug the old in-process version could have had).
    attempts = await record_verify_attempt(user_id)
    if attempts > settings.NOTIFY_VERIFY_MAX_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="Too many verification attempts. Try again in an hour.",
        )

    async with tenant_session(tenant_id) as session:
        redeemed = await redeem_token(session, payload.code, CHANNEL_WHATSAPP)
        if redeemed is None or redeemed.address is None:
            raise HTTPException(
                status_code=400, detail="That code is invalid or has expired."
            )
        destination_id = await create_destination(
            session,
            redeemed.tenant_id,
            redeemed.user_id,
            CHANNEL_WHATSAPP,
            redeemed.address,
        )

    return {"status": "verified", "destination_id": str(destination_id)}


@router.post("/notifications/destinations/whatsapp-linked")
async def whatsapp_linked_link(request: Request) -> dict:
    """Add the caller's own paired WhatsApp device as a destination.

    No code and no verification step, which is the whole difference from the
    Meta path above: scanning the QR to pair the device already proved
    possession of the number, and the number is not typed here at all — it is
    read from the session the gateway recorded.

    Idempotent. `create_destination` upserts on
    `(tenant_id, channel, address_hash)`, so asking twice returns the same row
    rather than violating that index — and re-linking after an unlink or a
    disable revives it.
    """
    user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        wa = (
            await session.execute(_MY_WA_SESSION, {"user_id": user_id})
        ).one_or_none()
        if wa is None or wa.status != WA_STATUS_CONNECTED or not wa.phone_e164:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No connected WhatsApp device. Pair your phone in "
                    "Settings → WhatsApp first."
                ),
            )
        destination_id = await create_destination(
            session,
            tenant_id,
            # Never null: the session is one recruiter's device, so there is no
            # agency-wide form of this destination for `set_scope` to make.
            user_id,
            CHANNEL_WHATSAPP_LINKED,
            wa.phone_e164,
        )

    return {"status": "linked", "destination_id": str(destination_id)}


@router.put("/notifications/destinations/{destination_id}/scope")
async def set_scope(
    request: Request, destination_id: uuid.UUID, payload: ScopeUpdate
) -> dict:
    """Point a destination at the agency rather than at one recruiter.

    Promoting sets `user_id` to null, which is what the dispatch query reads as
    "this belongs to the tenant". Demoting reattaches it to whoever is asking —
    the only person we can attribute it to from here.
    """
    user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        existing = (
            await session.execute(
                _DESTINATION_CHANNEL, {"id": destination_id, **_mine(user_id)}
            )
        ).one_or_none()
        if existing is not None and existing.channel == CHANNEL_WHATSAPP_LINKED:
            # A paired device belongs to one recruiter, so promoting it would
            # null out the `user_id` the send path needs to find the socket —
            # a destination that looks shared and can never send.
            raise HTTPException(
                status_code=400,
                detail="A linked WhatsApp device cannot be shared with the agency.",
            )
        updated = (
            await session.execute(
                _SET_SCOPE,
                {
                    "id": destination_id,
                    "new_user_id": None if payload.scope == "tenant" else user_id,
                    **_mine(user_id),
                },
            )
        ).one_or_none()
    if updated is None:
        raise HTTPException(status_code=404, detail="Destination not found.")
    return {"status": "updated", "scope": payload.scope}


@router.delete("/notifications/destinations/{destination_id}", status_code=204)
async def delete_destination(request: Request, destination_id: uuid.UUID) -> None:
    """Unlink. Subscriptions cascade with the destination."""
    user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        deleted = (
            await session.execute(
                _DELETE_DESTINATION, {"id": destination_id, **_mine(user_id)}
            )
        ).one_or_none()
    if deleted is None:
        raise HTTPException(status_code=404, detail="Destination not found.")
