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


class PermanentReason(StrEnum):
    """Why a PERMANENT outcome is permanent — the operator's question.

    `disable_destination` already answers "what should the caller do", but
    three quite different truths share its False branch and an operator
    reading one log line cannot act on them the same way. ADDRESS is the
    default because it is the ordinary case: the number is dead.
    """

    # This address will never accept a message again (bot-blocked, not on
    # WhatsApp). The destination is disabled.
    ADDRESS = "address"
    # A channel-wide misconfiguration — an unapproved template, say. Every
    # send on the channel is failing identically and a human has to fix it.
    CONFIG = "config"
    # The provider refused *this one message* on an otherwise healthy
    # connection. Nothing is broken; this message is not going out.
    REJECTED = "rejected"
    # We do not know whether it was delivered. `failed` is the safest status
    # to record — retrying risks a duplicate nobody can un-send (§15) — but
    # it may be untrue, and that is worth saying out loud rather than filing
    # under one of the certainties above.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    provider_message_id: str | None = None
    error: str | None = None
    # Seconds, from the provider's own Retry-After. Honouring it is what keeps
    # a throttled account from being throttled harder.
    retry_after: float | None = None
    # TRANSIENT only. True when the provider refused because we asked too
    # soon — send spacing, a rate cap — rather than because anything went
    # wrong. Nothing was dispatched and the next try is expected to succeed,
    # so `deliver_notification` re-queues these WITHOUT charging the row's
    # attempt budget: a refusal to go faster is the system working, not a
    # failed delivery. Charging it meant an evening burst of six spent the
    # tail's whole budget being told "not yet" and then reported the job
    # order as undeliverable. Bounded by wall clock instead — see
    # NOTIFY_BACKPRESSURE_DEADLINE_MINUTES.
    backpressure: bool = False
    # PERMANENT only; ignored otherwise. Chooses the log branch at the call
    # site, which is the only place that knows this is worth an operator's
    # attention.
    permanent_reason: PermanentReason = PermanentReason.ADDRESS
    # Defaults True: an address-level PERMANENT failure (bot-blocked, number
    # not on WhatsApp) means the destination itself is dead, so the caller
    # should disable it. A channel sets this False for a PERMANENT outcome
    # that is really a *configuration* problem — an unapproved WhatsApp
    # template, say — where the address is fine and retrying every message
    # to it would only repeat the same template error forever. Disabling
    # every destination on deploy day because a template has not been
    # approved yet is the failure this field exists to prevent; see
    # whatsapp.py's PERMANENT_ERROR_CODES.
    disable_destination: bool = True


class Channel(Protocol):
    """What `deliver_notification` depends on — never a concrete client.

    Tests substitute a fake implementing this and no test touches the network,
    the same seam `workers/queue.py` uses for `_create_pool`.
    """

    async def send(self, address: str, content) -> SendResult: ...
