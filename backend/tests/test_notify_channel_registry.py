"""`channel_for`: the one seam that picks a channel client by name.

No network, no database — this only checks which class comes back and that
the two pre-existing channels still work when called with no keyword
arguments at all, which is how every caller before `whatsapp_linked` existed
still calls it (see the many `lambda name: fake` monkeypatches across
test_deliver_notification.py, test_notifications_api.py,
test_opportunity_share_notifications.py and test_candidate_notifications.py).
"""

import uuid

import pytest

from app.models.notification import (
    CHANNEL_TELEGRAM,
    CHANNEL_WHATSAPP,
    CHANNEL_WHATSAPP_LINKED,
)
from app.services.notify.channels import channel_for
from app.services.notify.channels.telegram import TelegramChannel
from app.services.notify.channels.whatsapp import WhatsAppChannel
from app.services.notify.channels.whatsapp_linked import WhatsAppLinkedChannel


def test_telegram_needs_no_keyword_arguments() -> None:
    assert isinstance(channel_for(CHANNEL_TELEGRAM), TelegramChannel)


def test_whatsapp_needs_no_keyword_arguments() -> None:
    assert isinstance(channel_for(CHANNEL_WHATSAPP), WhatsAppChannel)


def test_whatsapp_linked_returns_the_linked_channel_carrying_the_caller_identity() -> None:
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    channel = channel_for(CHANNEL_WHATSAPP_LINKED, tenant_id=tenant_id, user_id=user_id)
    assert isinstance(channel, WhatsAppLinkedChannel)
    assert channel._tenant_id == tenant_id
    assert channel._user_id == user_id


def test_unknown_channel_raises() -> None:
    with pytest.raises(ValueError):
        channel_for("carrier-pigeon")
