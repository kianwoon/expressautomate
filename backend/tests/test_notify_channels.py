"""Channel clients, against a stub transport. Nothing here touches the network."""

import httpx

from app.core.config import settings
from app.services.notify.channels.base import SendOutcome
from app.services.notify.channels.telegram import TelegramChannel
from app.services.notify.channels.whatsapp import WhatsAppChannel
from app.services.notify.render import TelegramContent, WhatsAppContent


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _wa_content() -> WhatsAppContent:
    return WhatsAppContent(
        template_name="opportunity_new",
        language="en",
        body_params=["Engineer", "Acme", "Singapore", "SGD 8,000"],
        button_param="11111111-1111-1111-1111-111111111111",
    )


async def test_telegram_success_returns_the_message_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.SENT
    assert result.provider_message_id == "42"


async def test_telegram_403_is_permanent() -> None:
    """The recruiter blocked the bot. Retrying cannot change that."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "bot was blocked"})

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.PERMANENT


async def test_telegram_429_is_transient_and_carries_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "17"},
            json={"ok": False, "description": "Too Many Requests"},
        )

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.TRANSIENT
    assert result.retry_after == 17.0


async def test_telegram_500_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream is unwell")

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.TRANSIENT


async def test_whatsapp_posts_a_template_with_ordered_params() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    result = await WhatsAppChannel(client=_client(handler)).send(
        "+6591234567", _wa_content()
    )
    assert result.outcome is SendOutcome.SENT
    assert result.provider_message_id == "wamid.ABC"
    assert seen["type"] == "template"
    assert seen["template"]["name"] == "opportunity_new"
    body = next(c for c in seen["template"]["components"] if c["type"] == "body")
    assert [p["text"] for p in body["parameters"]] == [
        "Engineer",
        "Acme",
        "Singapore",
        "SGD 8,000",
    ]


async def test_whatsapp_131026_is_permanent() -> None:
    """Undeliverable — the number is not on WhatsApp. Never retry it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 131026, "message": "undeliverable"}})

    result = await WhatsAppChannel(client=_client(handler)).send(
        "+6591234567", _wa_content()
    )
    assert result.outcome is SendOutcome.PERMANENT


async def test_whatsapp_rate_limit_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 130429, "message": "rate limit"}})

    result = await WhatsAppChannel(client=_client(handler)).send(
        "+6591234567", _wa_content()
    )
    assert result.outcome is SendOutcome.TRANSIENT


async def test_network_failure_is_transient_not_a_crash() -> None:
    """A worker that raises here loses the claim and the row sticks in
    `sending` until the sweep finds it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.TRANSIENT


async def test_send_url_is_built_from_config() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

    await WhatsAppChannel(client=_client(handler)).send("+6591234567", _wa_content())
    assert seen["url"].startswith(settings.WHATSAPP_API_BASE_URL)
    assert settings.WHATSAPP_PHONE_NUMBER_ID in seen["url"]


async def test_telegram_200_with_invalid_json_is_transient() -> None:
    """Gateway returned 200 with unparseable body (e.g., HTML error page).
    Cannot determine if the message sent, so transient is the safe default."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error</html>")

    result = await TelegramChannel(client=_client(handler)).send(
        "12345", TelegramContent(text="hello")
    )
    assert result.outcome is SendOutcome.TRANSIENT
    assert result.error is not None


async def test_whatsapp_200_with_invalid_json_is_transient() -> None:
    """Gateway returned 200 with unparseable body (e.g., HTML error page).
    Cannot determine if the message sent, so transient is the safe default."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error</html>")

    result = await WhatsAppChannel(client=_client(handler)).send(
        "+6591234567", _wa_content()
    )
    assert result.outcome is SendOutcome.TRANSIENT
    assert result.error is not None
