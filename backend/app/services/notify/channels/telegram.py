"""Telegram Bot API.

Telegram has no template regime and no 24-hour window, so this sends prose.
The whole client is one POST.
"""

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.notify.channels._response import _json_object
from app.services.notify.channels.base import SendOutcome, SendResult
from app.services.notify.render import TelegramContent

log = get_logger(__name__)

# Telegram's own retry hint lives in the body, not only in the header.
_RETRY_AFTER_BODY_KEY = "retry_after"


class TelegramChannel:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Injected in tests. Constructing one here by default keeps every
        # caller from having to know about transports.
        self._client = client
        self._owns_client = client is None

    async def send(self, address: str, content: TelegramContent) -> SendResult:
        url = f"{settings.TELEGRAM_API_BASE_URL}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": address,
            "text": content.text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            # Never raise out of a channel. The caller has a claimed row to
            # release, and an exception here would leave it stuck in `sending`
            # until the sweep found it.
            return SendResult(outcome=SendOutcome.TRANSIENT, error=str(exc))
        finally:
            if self._owns_client:
                try:
                    await client.aclose()
                except httpx.HTTPError:
                    # A failure to close must not replace whatever result or
                    # exception is already in flight from the try block above —
                    # that would trade a real outcome for a spurious one and
                    # trip the same stuck-row invariant this whole method exists
                    # to protect.
                    pass

        return _interpret(response)


def _interpret(response: httpx.Response) -> SendResult:
    if response.status_code == 200:
        body = _json_object(response)
        if body is None:
            # Covers both an unparseable body (e.g., HTML error page) and a
            # body that parsed but wasn't an object (`null`, `[]`, `"x"`).
            # Transient because we cannot determine if the message sent, so
            # retry is safe — see _json_object's docstring for why this is
            # one check instead of two.
            return SendResult(
                outcome=SendOutcome.TRANSIENT,
                error="invalid or unexpected response shape",
            )
        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return SendResult(
            outcome=SendOutcome.SENT,
            provider_message_id=str(message_id) if message_id is not None else None,
        )

    detail = _describe(response)

    if response.status_code == 429:
        # `backpressure=True` for the same reason as the WA gateway's spacing
        # refusal (see base.py): Telegram dispatched nothing and is only
        # telling us to slow down, so this must not spend the row's attempt
        # budget. Telegram's per-chat limit is far above our own hourly cap,
        # so this is the rarer of the two paths — but the defect is the same
        # one, and a channel that only mostly cannot hit it is not a reason
        # to leave it able to drop a notification.
        return SendResult(
            outcome=SendOutcome.TRANSIENT,
            error=detail,
            retry_after=_retry_after(response),
            backpressure=True,
        )
    if response.status_code >= 500:
        return SendResult(outcome=SendOutcome.TRANSIENT, error=detail)
    # 400 (chat not found), 403 (bot blocked). Both mean this destination will
    # never accept a message again; retrying is throughput spent on nothing.
    return SendResult(outcome=SendOutcome.PERMANENT, error=detail)


def _describe(response: httpx.Response) -> str:
    body = _json_object(response)
    if body is None:
        return response.text[:500]
    return str(body.get("description", response.text))[:500]


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    body = _json_object(response) or {}
    parameters = body.get("parameters")
    if not isinstance(parameters, dict):
        # Same shape gap one level deeper: `parameters` is documented as an
        # object but nothing stops a 429 body from sending it as something
        # else, and `.get` on that would raise exactly like the top-level
        # cases this helper exists to prevent.
        return None
    value = parameters.get(_RETRY_AFTER_BODY_KEY)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
