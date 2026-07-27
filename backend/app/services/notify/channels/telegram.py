"""Telegram Bot API.

Telegram has no template regime and no 24-hour window, so this sends prose.
The whole client is one POST.
"""

import httpx

from app.core.config import settings
from app.core.logging import get_logger
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
                await client.aclose()

        return _interpret(response)


def _interpret(response: httpx.Response) -> SendResult:
    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError as exc:
            # Gateway returned 200 with unparseable body (e.g., HTML error page).
            # Transient because we cannot determine if the message sent, so retry is safe.
            return SendResult(
                outcome=SendOutcome.TRANSIENT,
                error=f"invalid response: {str(exc)[:500]}"
            )
        message_id = body.get("result", {}).get("message_id")
        return SendResult(
            outcome=SendOutcome.SENT,
            provider_message_id=str(message_id) if message_id is not None else None,
        )

    detail = _describe(response)

    if response.status_code == 429:
        return SendResult(
            outcome=SendOutcome.TRANSIENT, error=detail, retry_after=_retry_after(response)
        )
    if response.status_code >= 500:
        return SendResult(outcome=SendOutcome.TRANSIENT, error=detail)
    # 400 (chat not found), 403 (bot blocked). Both mean this destination will
    # never accept a message again; retrying is throughput spent on nothing.
    return SendResult(outcome=SendOutcome.PERMANENT, error=detail)


def _describe(response: httpx.Response) -> str:
    try:
        return str(response.json().get("description", response.text))[:500]
    except ValueError:
        return response.text[:500]


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        value = response.json().get("parameters", {}).get(_RETRY_AFTER_BODY_KEY)
        return float(value) if value is not None else None
    except (ValueError, AttributeError):
        return None
