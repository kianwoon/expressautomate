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
from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP
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
_LIST_DESTINATIONS = text(
    """
    SELECT d.id, d.channel, d.user_id, d.verified_at, d.disabled_at,
           coalesce(
               array_agg(s.event_kind) FILTER (WHERE s.active), ARRAY[]::text[]
           ) AS event_kinds
    FROM notification_destinations d
    LEFT JOIN notification_subscriptions s ON s.destination_id = d.id
    GROUP BY d.id
    ORDER BY d.created_at
    """
)

_DESTINATION_EXISTS = text(
    "SELECT id FROM notification_destinations WHERE id = :id"
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
    "DELETE FROM notification_destinations WHERE id = :id RETURNING id"
)

_SET_SCOPE = text(
    """
    UPDATE notification_destinations
    SET user_id = :user_id
    WHERE id = :id
    RETURNING id
    """
)


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
    _user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        rows = (await session.execute(_LIST_DESTINATIONS)).all()

    return {
        "events": [{"kind": kind} for kind in ALL_EVENT_KINDS],
        "channels": {
            CHANNEL_TELEGRAM: settings.telegram_configured(),
            CHANNEL_WHATSAPP: settings.whatsapp_configured(),
        },
        "destinations": [
            {
                "id": str(row.id),
                "channel": row.channel,
                # Null means the agency's shared feed rather than one person's.
                "scope": "tenant" if row.user_id is None else "user",
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
    _user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        exists = (
            await session.execute(
                _DESTINATION_EXISTS, {"id": payload.destination_id}
            )
        ).one_or_none()
        if exists is None:
            # Under RLS another tenant's destination reads as absent, which is
            # the honest answer: it does not exist for this caller.
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
        updated = (
            await session.execute(
                _SET_SCOPE,
                {
                    "id": destination_id,
                    "user_id": None if payload.scope == "tenant" else user_id,
                },
            )
        ).one_or_none()
    if updated is None:
        raise HTTPException(status_code=404, detail="Destination not found.")
    return {"status": "updated", "scope": payload.scope}


@router.delete("/notifications/destinations/{destination_id}", status_code=204)
async def delete_destination(request: Request, destination_id: uuid.UUID) -> None:
    """Unlink. Subscriptions cascade with the destination."""
    _user_id, tenant_id = _require_session(request)
    async with tenant_session(tenant_id) as session:
        deleted = (
            await session.execute(_DELETE_DESTINATION, {"id": destination_id})
        ).one_or_none()
    if deleted is None:
        raise HTTPException(status_code=404, detail="Destination not found.")
