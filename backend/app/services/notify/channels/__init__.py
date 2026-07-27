"""Channel registry.

Imports are inside `channel_for` so a module-level import of this package does
not require either provider's configuration — the API process needs the
registry to validate a channel name long before it needs a client.
"""

from app.services.notify.channels.base import Channel, SendOutcome, SendResult

__all__ = ["Channel", "SendOutcome", "SendResult", "channel_for"]


def channel_for(name: str) -> Channel:
    from app.models.notification import CHANNEL_TELEGRAM, CHANNEL_WHATSAPP
    from app.services.notify.channels.telegram import TelegramChannel
    from app.services.notify.channels.whatsapp import WhatsAppChannel

    if name == CHANNEL_TELEGRAM:
        return TelegramChannel()
    if name == CHANNEL_WHATSAPP:
        return WhatsAppChannel()
    raise ValueError(f"Unknown notification channel: {name!r}")
