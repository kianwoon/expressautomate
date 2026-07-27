"""Telegram bot updates (spec 2026-07-28).

Follows `graph_webhook.py`: validate, do the smallest durable thing, answer
fast. Every path returns 200 once the secret checks out — Telegram retries a
non-2xx, and none of the failures here are worth retrying.

The token names its own tenant, which is the only way this request has of
knowing one — a `/start` arrives with no session and no cookie. Resolving it
therefore needs a read with no tenant set, and that read goes through
`resolve_link_token_tenant`, a narrow `SECURITY DEFINER` function (see the
`resolve_link_token_tenant` migration), not a second RLS policy on
`notification_link_tokens`. A permissive policy would stay live for *any*
unscoped connection for as long as the table exists; the function is scoped
to exactly the one query this path needs and returns exactly one column —
the tenant id, nothing else — so everything else (user id, address) is
re-read by `redeem_token` once a tenant-scoped session is open.
"""

import secrets
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.db.session import SessionLocal
from app.models.notification import CHANNEL_TELEGRAM, address_digest
from app.services.notify.linking import LinkedToken, create_destination, redeem_token

log = get_logger(__name__)

router = APIRouter(tags=["webhooks"])

_START = "/start"

_RESOLVE_TENANT = text("SELECT resolve_link_token_tenant(:token_hash, :channel)")


@asynccontextmanager
async def _redeem_under_its_own_tenant(token: str):
    """Resolve `token` to its tenant and redeem it there, or yield `None`.

    Two round trips on purpose: the first (unscoped, via the SECURITY
    DEFINER resolver) only ever answers "which tenant", so it cannot leak a
    user id or address even to a caller who fully controls the request. The
    second opens an ordinary tenant-scoped session and redeems the token
    exactly as any other caller of `redeem_token` would — under the same RLS
    policy as the rest of this table, not around it.
    """
    async with SessionLocal() as lookup:
        tenant_id = (
            await lookup.execute(
                _RESOLVE_TENANT,
                {"token_hash": address_digest(token), "channel": CHANNEL_TELEGRAM},
            )
        ).scalar_one_or_none()

    if tenant_id is None:
        yield None
        return

    async with tenant_session(tenant_id) as session:
        redeemed = await redeem_token(session, token, CHANNEL_TELEGRAM)
        if redeemed is None:
            # Resolved a moment ago, gone now — someone else redeemed the
            # same token in the race between the two round trips. Nothing to
            # link; the other request already did.
            yield None
            return
        yield session, redeemed


@router.post("/webhooks/telegram")
async def telegram_update(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, str]:
    """Resolve a `/start <token>` into a verified destination.

    This URL is public and unauthenticated by construction — Telegram will not
    carry a cookie — so the shared secret is the entire gate. Compared with
    `compare_digest`, because a plain `==` leaks its answer through timing. An
    empty configured secret always rejects: `expected` falsy means nothing was
    ever configured, and comparing an offered value against "" would let a
    caller send an empty header and pass.
    """
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    if not expected or not x_telegram_bot_api_secret_token:
        raise HTTPException(status_code=401, detail="Unauthorised.")
    if not secrets.compare_digest(x_telegram_bot_api_secret_token, expected):
        raise HTTPException(status_code=401, detail="Unauthorised.")

    try:
        payload = await request.json()
    except Exception:
        # Malformed JSON. A 4xx/5xx would have Telegram retry a body that
        # will never parse.
        log.warning("telegram_unparseable_body")
        return {"status": "ignored"}

    message = payload.get("message") if isinstance(payload, dict) else None
    message = message or {}
    text_value = message.get("text") or ""
    chat_id = (message.get("chat") or {}).get("id")

    if not text_value.startswith(_START) or chat_id is None:
        # Someone talking to the bot. There is nothing for us in it.
        return {"status": "ignored"}

    parts = text_value.split(maxsplit=1)
    if len(parts) < 2:
        return {"status": "ignored"}
    token = parts[1].strip()

    async with _redeem_under_its_own_tenant(token) as scoped:
        if scoped is None:
            log.info("telegram_link_token_unknown")
            return {"status": "ignored"}
        session, redeemed = scoped
        await _link(session, redeemed, chat_id)

    log.info("telegram_destination_linked")
    return {"status": "linked"}


async def _link(session, redeemed: LinkedToken, chat_id: int) -> uuid.UUID:
    return await create_destination(
        session, redeemed.tenant_id, redeemed.user_id, CHANNEL_TELEGRAM, str(chat_id)
    )
