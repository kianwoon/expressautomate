"""Meta WhatsApp Cloud API.

Every message this sends is business-initiated outside any customer service
window, so it is always a pre-approved template — there is no free-form path
here and adding one would produce sends Meta rejects at the edge.
"""

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.notify.channels.base import SendOutcome, SendResult
from app.services.notify.render import WhatsAppContent

log = get_logger(__name__)

# Meta error codes that will never succeed on retry. Anything not listed is
# treated as transient, which is the safe default: a retried transient costs
# one extra call, a retried permanent costs every call forever.
PERMANENT_ERROR_CODES = frozenset(
    {
        131026,  # Message undeliverable — the number is not a WhatsApp user.
        131047,  # Re-engagement required; a template cannot open this window.
        131051,  # Unsupported message type.
        132000,  # Template param count does not match the approved template.
        132001,  # Template does not exist in this language.
        132015,  # Template is paused for quality reasons.
        132016,  # Template has been disabled.
        133010,  # Phone number not registered.
    }
)


class WhatsAppChannel:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def send(self, address: str, content: WhatsAppContent) -> SendResult:
        url = f"{settings.WHATSAPP_API_BASE_URL}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": address,
            "type": "template",
            "template": {
                "name": content.template_name,
                "language": {"code": content.language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": value}
                            for value in content.body_params
                        ],
                    },
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [{"type": "text", "text": content.button_param}],
                    },
                ],
            },
        }
        headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}

        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return SendResult(outcome=SendOutcome.TRANSIENT, error=str(exc))
        finally:
            if self._owns_client:
                await client.aclose()

        return _interpret(response)


def _interpret(response: httpx.Response) -> SendResult:
    if response.status_code == 200:
        body = response.json()
        messages = body.get("messages") or []
        return SendResult(
            outcome=SendOutcome.SENT,
            provider_message_id=messages[0].get("id") if messages else None,
        )

    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    code = error.get("code")
    detail = str(error.get("message", response.text))[:500]

    if code in PERMANENT_ERROR_CODES:
        return SendResult(outcome=SendOutcome.PERMANENT, error=detail)
    if response.status_code == 429 or response.status_code >= 500:
        return SendResult(
            outcome=SendOutcome.TRANSIENT, error=detail, retry_after=_retry_after(response)
        )
    # An unrecognised 4xx. Transient by default — see PERMANENT_ERROR_CODES.
    return SendResult(outcome=SendOutcome.TRANSIENT, error=detail)


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    try:
        return float(header) if header else None
    except ValueError:
        return None
