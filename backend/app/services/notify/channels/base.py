"""The shape every channel presents, and the three answers a send can give.

Three outcomes rather than a boolean, because the caller's next move differs
completely. TRANSIENT retries. PERMANENT must not — retrying a number that is
not on WhatsApp burns quota forever and never succeeds — and it disables the
destination so a dead address becomes visible instead of absorbing messages.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class SendOutcome(StrEnum):
    SENT = "sent"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    provider_message_id: str | None = None
    error: str | None = None
    # Seconds, from the provider's own Retry-After. Honouring it is what keeps
    # a throttled account from being throttled harder.
    retry_after: float | None = None


class Channel(Protocol):
    """What `deliver_notification` depends on — never a concrete client.

    Tests substitute a fake implementing this and no test touches the network,
    the same seam `workers/queue.py` uses for `_create_pool`.
    """

    async def send(self, address: str, content) -> SendResult: ...
