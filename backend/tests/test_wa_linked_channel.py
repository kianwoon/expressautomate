"""WhatsAppLinkedChannel outcome mapping, against a fake WaGatewayClient.

Separate file rather than folding into test_notify_channels.py: that file
tests the two HTTP-transport channels (Telegram, WhatsApp Cloud API) against a
stubbed `httpx.MockTransport`; this channel has no HTTP transport of its own —
it wraps `WaGatewayClient.send`, which is faked directly here — so the fixture
shape is different enough to earn its own module.

The send-target tests at the bottom need a real `wa_sessions` row (the channel
resolves the self-chat number from the database), so they use the database
fixtures rather than the pure fake.
"""

import uuid

import pytest
from sqlalchemy import text

from app.models.wa_session import (
    STATUS_CONNECTED,
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


# --- Send-target resolution (the re-pair bug) -----------------------------
#
# The channel must send to the recruiter's *current* paired number
# (`wa_sessions.phone_e164`), never to the destination's stored address: a
# recruiter who re-pairs with a second number keeps a `whatsapp_linked`
# destination pointing at the first, and messaging the stored address is how
# the old device keeps receiving notifications after the switch. These tests
# seed a real `wa_sessions` row and assert the fake gateway is asked for the
# live number.


async def _seed_session(
    admin_session, tenant_id: uuid.UUID, user_id: uuid.UUID, phone_e164: str | None, status: str
) -> None:
    await admin_session.execute(
        text(
            "INSERT INTO wa_sessions (id, tenant_id, user_id, status, phone_e164) "
            "VALUES (:id, :tid, :uid, :status, :phone)"
        ),
        {
            "id": user_id,
            "tid": tenant_id,
            "uid": user_id,
            "status": status,
            "phone": phone_e164,
        },
    )
    await admin_session.commit()


async def test_send_uses_the_live_session_number_when_the_stored_address_is_stale(
    admin_session,
) -> None:
    """The regression test for "the first WhatsApp number still receives
    notifications". The destination row still carries the old number; the
    session says the recruiter's device is now a different one. The send must
    go to the live number — the recruiter's own current device — not to the
    stale stored address that would reach the previous device."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"
        ),
        {"id": tenant_id, "slug": f"agency-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'r@a.sg', 'recruiter')"
        ),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.commit()
    await _seed_session(
        admin_session,
        tenant_id,
        user_id,
        phone_e164="+6598765432",  # the NEW number, after re-pair
        status=STATUS_CONNECTED,
    )

    client = _FakeGatewayClient(
        outcome=GatewaySendOutcome(
            ok=True, session_status="connected", provider_message_id="wamid.1"
        )
    )
    channel = WhatsAppLinkedChannel(tenant_id, user_id, client=client)
    # The stored address (the old number) is what the delivery job would pass;
    # the channel must override it with the live session number.
    result = await channel.send("+6591234567", TelegramContent(text="hi"))

    assert result.outcome is SendOutcome.SENT
    assert client.calls == [(str(tenant_id), str(user_id), "+6598765432", "hi")]

    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def test_send_falls_back_to_the_stored_address_when_the_session_has_no_number(
    admin_session,
) -> None:
    """A connected session with a NULL phone is a state the gateway should
    never report, but if it happens the send must not crash — the stored
    address is the only number available and the gateway's own liveness check
    is what decides the outcome."""
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
        {"id": tenant_id, "slug": f"agency-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'r@a.sg', 'recruiter')"
        ),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.commit()
    await _seed_session(admin_session, tenant_id, user_id, phone_e164=None, status=STATUS_CONNECTED)

    client = _FakeGatewayClient(
        outcome=GatewaySendOutcome(ok=False, session_status=STATUS_RECONNECTING)
    )
    channel = WhatsAppLinkedChannel(tenant_id, user_id, client=client)
    result = await channel.send("+6591234567", TelegramContent(text="hi"))

    assert result.outcome is SendOutcome.TRANSIENT
    # The stored address was used because there was no live number to prefer.
    assert client.calls == [(str(tenant_id), str(user_id), "+6591234567", "hi")]

    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()
