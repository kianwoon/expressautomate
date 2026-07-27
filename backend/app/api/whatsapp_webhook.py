"""Meta delivery statuses and opt-outs (spec 2026-07-28).

Nothing else tells us a message failed. The send call returns 200 when Meta
*accepts* a message, not when it arrives — so without this endpoint, "a
permanent failure disables the destination" (§ the notify pipeline) has no
input and never fires.

An opt-out is a fact about our shared phone number, not about one agency:
Meta counts continued sends against the number every tenant shares, so one
recruiter's STOP has to stop every tenant messaging them. That is why
`whatsapp_suppressions` carries no `tenant_id` (see its model docstring) and
why this handler writes to it with no tenant set.

A status callback is the opposite shape: it names a `provider_message_id`
and no tenant at all, but the rows it touches — the delivery, and the
destination it disables on a permanent failure — *are* tenant data. Both
writes go through `fail_whatsapp_delivery`, a narrow `SECURITY DEFINER`
function (see its migration) rather than a second RLS policy on
`notification_deliveries` — the same choice `resolve_link_token_tenant` made
for the Telegram link table, for the same reason: a permissive policy stays
live for any unscoped connection for as long as the table exists, and this
table is more sensitive than that one.

Every path here answers 200 once the signature checks out. Meta retries a
non-2xx and disables a webhook that keeps failing, which would cost us the
status feed entirely — so a malformed payload is logged and ignored, never
raised.
"""

import hashlib
import hmac

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.notification import address_digest

log = get_logger(__name__)

router = APIRouter(tags=["webhooks"])

# What a person types to be left alone, matched against the *whole* trimmed,
# lowercased message — never as a substring. "stop by the office tomorrow"
# must not opt someone out.
# allow-hardcode: recognised opt-out keywords, Meta's own WhatsApp convention.
_OPT_OUT_WORDS = frozenset({"stop", "unsubscribe", "cancel"})

# Unscoped, and correctly so: see the module docstring and the
# WhatsAppSuppression model docstring. Its `global_read` policy (`USING
# (true) WITH CHECK (true)`) already permits an unscoped INSERT — no
# SECURITY DEFINER function is needed here, unlike the deliveries table.
# allow-hardcode: SQL statement text, not a data value.
_SUPPRESS = text(
    """
    INSERT INTO whatsapp_suppressions (id, address_hash, reason)
    VALUES (gen_random_uuid(), :address_hash, :reason)
    ON CONFLICT (address_hash) DO NOTHING
    """
)

# Also disables the matched delivery's destination, in the same statement —
# see the function's migration for why that write has to live here too
# rather than as a second, separately unscoped UPDATE in this module.
# allow-hardcode: SQL statement text, not a data value.
_FAIL_DELIVERY = text("SELECT * FROM fail_whatsapp_delivery(:provider_message_id, :error)")


@router.get("/webhooks/whatsapp")
async def verify(request: Request) -> Response:
    """Meta's one-time subscription handshake.

    Meta expects the raw challenge echoed back as plain text; answering with
    JSON fails the handshake with no explanation on either side.
    """
    params = request.query_params
    if params.get("hub.verify_token") != settings.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Verification failed.")
    return Response(content=params.get("hub.challenge", ""), media_type="text/plain")


@router.post("/webhooks/whatsapp")
async def receive(request: Request) -> dict[str, str]:
    """Statuses and inbound messages.

    The signature is over the *raw* body, so it is read as bytes before
    anything parses it — re-serialising the parsed JSON would change the
    bytes, and the digest with them.
    """
    raw = await request.body()
    _verify_signature(raw, request.headers.get("X-Hub-Signature-256"))

    try:
        payload = await request.json()
    except ValueError:
        # Signed but not valid JSON. Logged, not raised: a 5xx here is what
        # eventually gets this webhook disabled by Meta.
        log.warning("whatsapp_webhook_unparseable_body")
        return {"status": "ignored"}

    if not isinstance(payload, dict):
        return {"status": "ignored"}

    entries = payload.get("entry")
    if not isinstance(entries, list):
        return {"status": "ignored"}

    for entry in entries:
        changes = entry.get("changes") if isinstance(entry, dict) else None
        if not isinstance(changes, list):
            continue
        for change in changes:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            statuses = value.get("statuses")
            if isinstance(statuses, list):
                await _handle_statuses(statuses)
            messages = value.get("messages")
            if isinstance(messages, list):
                await _handle_messages(messages)

    return {"status": "ok"}


def _verify_signature(raw: bytes, header: str | None) -> None:
    """Reject anything not signed by the configured app secret.

    A missing or empty configured secret must reject rather than pass: an
    `expected` that is falsy means nothing was ever configured, and a plain
    truthiness check against an offered header would otherwise let an
    unconfigured deployment accept unsigned traffic.
    """
    secret = settings.WHATSAPP_APP_SECRET
    if not secret or not header or not header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Unauthorised.")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    # compare_digest, because a plain `==` leaks the answer through timing.
    if not hmac.compare_digest(header.removeprefix("sha256="), expected):
        raise HTTPException(status_code=401, detail="Unauthorised.")


async def _handle_statuses(statuses: list) -> None:
    for status in statuses:
        if not isinstance(status, dict) or status.get("status") != "failed":
            # `sent` / `delivered` / `read` — the row is already correct —
            # or a malformed entry, which is simply skipped.
            continue
        provider_message_id = status.get("id")
        if not provider_message_id:
            continue
        errors = status.get("errors")
        first_error = errors[0] if isinstance(errors, list) and errors else {}
        detail = f"{first_error.get('code')}: {first_error.get('title')}"

        async with SessionLocal() as session:
            await session.execute(
                _FAIL_DELIVERY,
                {"provider_message_id": provider_message_id, "error": detail[:500]},
            )
            await session.commit()
        log.warning("whatsapp_delivery_failed", error=detail)


async def _handle_messages(messages: list) -> None:
    for message in messages:
        if not isinstance(message, dict):
            continue
        text_block = message.get("text")
        body = (text_block or {}).get("body", "") if isinstance(text_block, dict) else ""
        if not isinstance(body, str) or body.strip().lower() not in _OPT_OUT_WORDS:
            continue
        sender = message.get("from")
        if not sender:
            continue
        # Meta reports the sender without a leading '+'; our stored
        # addresses carry one, so the digest must match what we stored.
        async with SessionLocal() as session:
            await session.execute(
                _SUPPRESS,
                {
                    "address_hash": address_digest(f"+{str(sender).lstrip('+')}"),
                    "reason": "user_stop",
                },
            )
            await session.commit()
        log.info("whatsapp_opt_out_recorded")
