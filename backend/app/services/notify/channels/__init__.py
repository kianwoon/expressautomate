"""Channel registry.

Imports are inside `channel_for` so a module-level import of this package does
not require either provider's configuration — the API process needs the
registry to validate a channel name long before it needs a client.
"""

import uuid

from app.services.notify.channels.base import Channel, SendOutcome, SendResult

__all__ = ["Channel", "SendOutcome", "SendResult", "channel_for"]


def channel_for(
    name: str,
    *,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> Channel:
    """The client for one channel name.

    `tenant_id`/`user_id` exist for `whatsapp_linked` alone, which sends on one
    recruiter's own paired device and so needs to know whose. Keyword-only and
    defaulted, because the other two channels have no session to identify and
    every existing caller passes neither.
    """
    from app.models.notification import (
        CHANNEL_TELEGRAM,
        CHANNEL_WHATSAPP,
        CHANNEL_WHATSAPP_LINKED,
    )
    from app.services.notify.channels.telegram import TelegramChannel
    from app.services.notify.channels.whatsapp import WhatsAppChannel
    from app.services.notify.channels.whatsapp_linked import WhatsAppLinkedChannel

    if name == CHANNEL_TELEGRAM:
        return TelegramChannel()
    if name == CHANNEL_WHATSAPP:
        return WhatsAppChannel()
    if name == CHANNEL_WHATSAPP_LINKED:
        return WhatsAppLinkedChannel(tenant_id, user_id)
    raise ValueError(f"Unknown notification channel: {name!r}")
