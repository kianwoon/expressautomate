"""WhatsAppLinkedChannel outcome mapping, against a fake WaGatewayClient.

Separate file rather than folding into test_notify_channels.py: that file
tests the two HTTP-transport channels (Telegram, WhatsApp Cloud API) against a
stubbed `httpx.MockTransport`; this channel has no HTTP transport of its own —
it wraps `WaGatewayClient.send`, which is faked directly here — so the fixture
shape is different enough to earn its own module.
"""

import uuid

import pytest

from app.models.wa_session import (
    STATUS_DISCONNECTED,
    STATUS_LOGGED_OUT,
    STATUS_PAIRING,
    STATUS_RECONNECTING,
)
from app.services.notify.channels.base import SendOutcome
from app.services.notify.channels.whatsapp_linked import WhatsAppLinkedChannel
from app.services.notify.render import TelegramContent
from app.services.wa_gateway import (
    GatewayOutcomeUnknownError,
    GatewayRefusedError,
    GatewaySpacingError,
    GatewayUnreachableError,
)
from app.services.wa_gateway import (
    SendOutcome as GatewaySendOutcome,
)

_TENANT_ID = uuid.uuid4()
_USER_ID = uuid.uuid4()


class _FakeGatewayClient:
    """Answers `.send()` with whatever the test scripted, and never touches
    the network — this is the seam `WhatsAppLinkedChannel` is built around."""

    def __init__(self, outcome=None, raises: Exception | None = None) -> None:
        self._outcome = outcome
        self._raises = raises
        self.calls: list[tuple[str, str, str, str]] = []

    async def send(self, tenant_id: str, user_id: str, *, to: str, text: str):
        self.calls.append((tenant_id, user_id, to, text))
        if self._raises is not None:
            raise self._raises
        return self._outcome


def _channel(client: _FakeGatewayClient) -> WhatsAppLinkedChannel:
    return WhatsAppLinkedChannel(_TENANT_ID, _USER_ID, client=client)


async def test_ok_true_is_sent_with_the_provider_message_id() -> None:
    client = _FakeGatewayClient(
        outcome=GatewaySendOutcome(
            ok=True, session_status="connected", provider_message_id="wamid.ABC"
        )
    )
    result = await _channel(client).send("+6591234567", TelegramContent(text="hi"))
    assert result.outcome is SendOutcome.SENT
    assert result.provider_message_id == "wamid.ABC"
    assert client.calls == [(str(_TENANT_ID), str(_USER_ID), "+6591234567", "hi")]


async def test_reconnecting_is_transient() -> None:
    """The session comes back on its own; a send that lands here should retry."""
    client = _FakeGatewayClient(
        outcome=GatewaySendOutcome(ok=False, session_status=STATUS_RECONNECTING)
    )
    result = await _channel(client).send("+6591234567", TelegramContent(text="hi"))
    assert result.outcome is SendOutcome.TRANSIENT


async def test_pairing_is_transient() -> None:
    client = _FakeGatewayClient(
        outcome=GatewaySendOutcome(ok=False, session_status=STATUS_PAIRING)
    )
    result = await _channel(client).send("+6591234567", TelegramContent(text="hi"))
    assert result.outcome is SendOutcome.TRANSIENT


async def test_logged_out_is_permanent_and_disables_the_destination() -> None:
    """The device is gone; the recruiter has to re-pair by hand."""
    client = _FakeGatewayClient(
        outcome=GatewaySendOutcome(ok=False, session_status=STATUS_LOGGED_OUT)
    )
    result = await _channel(client).send("+6591234567", TelegramContent(text="hi"))
    assert result.outcome is SendOutcome.PERMANENT
    assert result.disable_destination is True


async def test_disconnected_is_permanent_and_disables_the_destination() -> None:
    client = _FakeGatewayClient(
        outcome=GatewaySendOutcome(ok=False, session_status=STATUS_DISCONNECTED)
    )
    result = await _channel(client).send("+6591234567", TelegramContent(text="hi"))
    assert result.outcome is SendOutcome.PERMANENT
    assert result.disable_destination is True


async def test_spacing_error_is_transient_and_carries_retry_after() -> None:
    client = _FakeGatewayClient(raises=GatewaySpacingError(23))
    result = await _channel(client).send("+6591234567", TelegramContent(text="hi"))
    assert result.outcome is SendOutcome.TRANSIENT
    assert result.retry_after == 23.0


async def test_refused_is_permanent_does_not_disable_and_keeps_the_gateways_words() -> None:
    """WhatsApp refused this message on a live socket — the number is fine,
    so the destination stays enabled, and the reason stored is the gateway's
    own wording, not a paraphrase (§15)."""
    client = _FakeGatewayClient(raises=GatewayRefusedError("recipient blocked this number"))
    result = await _channel(client).send("+6591234567", TelegramContent(text="hi"))
    assert result.outcome is SendOutcome.PERMANENT
    assert result.disable_destination is False
    assert result.error == "recipient blocked this number"


async def test_outcome_unknown_is_permanent_does_not_disable_and_does_not_raise() -> None:
    """The stuck-row invariant telegram.py protects: an exception out of a
    channel's `send` leaves the caller's claimed row stranded in `sending`
    (see base.py's Channel protocol docstring and telegram.py's send()). This
    is the assertion that actually pins that invariant, not just the outcome
    value."""
    client = _FakeGatewayClient(raises=GatewayOutcomeUnknownError("ambiguous 5xx"))
    try:
        result = await _channel(client).send("+6591234567", TelegramContent(text="hi"))
    except Exception as exc:  # noqa: BLE001 — the invariant under test is "never raises"
        pytest.fail(f"WhatsAppLinkedChannel.send raised {exc!r} instead of returning a SendResult")
    assert result.outcome is SendOutcome.PERMANENT
    assert result.disable_destination is False


async def test_unreachable_is_transient() -> None:
    """Certain to have dispatched nothing, so retrying is free of the
    duplicate risk GatewayOutcomeUnknownError exists to avoid."""
    client = _FakeGatewayClient(raises=GatewayUnreachableError("connection refused"))
    result = await _channel(client).send("+6591234567", TelegramContent(text="hi"))
    assert result.outcome is SendOutcome.TRANSIENT


async def test_no_user_attached_is_permanent_without_calling_the_gateway() -> None:
    """A destination with no owning recruiter cannot be sent to at all."""
    client = _FakeGatewayClient(outcome=GatewaySendOutcome(ok=True, session_status="connected"))
    channel = WhatsAppLinkedChannel(None, None, client=client)
    result = await channel.send("+6591234567", TelegramContent(text="hi"))
    assert result.outcome is SendOutcome.PERMANENT
    assert client.calls == []
