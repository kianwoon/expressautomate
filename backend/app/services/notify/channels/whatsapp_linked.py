"""WhatsApp over the recruiter's own paired device (spec 2026-07-29 §7).

Nothing here touches the Meta Cloud API. The message goes out on the
recruiter's own Baileys socket to their own number — a self-chat — so there is
no template regime and no 24-hour window, which is why this channel renders
the free-form Telegram prose rather than `WhatsAppContent`.

The `Channel` protocol hands a send only `(address, content)`, and the gateway
needs to know *whose* socket to use. That context is therefore given at
construction: `channel_for` accepts `tenant_id`/`user_id`, and the worker
passes the destination's own. The alternative — widening the protocol — would
put a session identity into the signature of two channels that have no
sessions.
"""

import uuid

from app.core.logging import get_logger
from app.models.wa_session import (
    STATUS_DISCONNECTED,
    STATUS_LOGGED_OUT,
)
from app.services.notify.channels.base import (
    PermanentReason,
    SendOutcome,
    SendResult,
)
from app.services.notify.render import TelegramContent
from app.services.wa_gateway import (
    GatewayOutcomeUnknownError,
    GatewayRefusedError,
    GatewaySpacingError,
    GatewayUnreachableError,
    WaGatewayClient,
)

log = get_logger(__name__)

# The session states a send will never recover from on its own: the device is
# gone and the recruiter has to re-pair. `pairing` and `reconnecting` are
# deliberately absent — those come back by themselves, so a send that lands in
# them is transient.
_DEAD_SESSION_STATUSES = frozenset({STATUS_DISCONNECTED, STATUS_LOGGED_OUT})


class WhatsAppLinkedChannel:
    def __init__(
        self,
        tenant_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
        client: WaGatewayClient | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._client = client or WaGatewayClient()

    async def send(self, address: str, content: TelegramContent) -> SendResult:
        """One notification to the recruiter's own number.

        Never raises — the caller holds a claimed outbox row, and an exception
        out of a channel is what leaves it stranded in `sending` (the same
        invariant `telegram.py` and `whatsapp.py` protect).
        """
        if self._tenant_id is None or self._user_id is None:
            # There is no agency-wide paired device, so a destination with no
            # user is unsendable rather than temporarily unavailable. Disable
            # it, per base.py: a dead destination should be visible instead of
            # absorbing messages.
            return SendResult(
                outcome=SendOutcome.PERMANENT,
                error="This WhatsApp destination is not attached to a recruiter.",
            )

        try:
            outcome = await self._client.send(
                str(self._tenant_id), str(self._user_id), to=address, text=content.text
            )
        except GatewaySpacingError as exc:
            # Plan §9's per-session send spacing. Nothing was dispatched, and
            # the gateway computed the wait against its own jittered deadline —
            # so honour that number rather than arq's backoff.
            #
            # `backpressure=True` is what stops a burst losing its tail: the
            # spacing floor is tens of seconds and the attempt budget is five,
            # so the sixth notification of an evening would otherwise exhaust
            # itself on refusals that were never failures. See base.py.
            return SendResult(
                outcome=SendOutcome.TRANSIENT,
                error=str(exc),
                retry_after=float(exc.retry_after_seconds),
                backpressure=True,
            )
        except GatewayRefusedError as exc:
            # WhatsApp refused *this message* on a live socket. Retrying it
            # cannot help, but the number is fine — same reasoning as
            # whatsapp.py's PERMANENT_CONFIG_ERROR_CODES, so the destination
            # stays enabled. The gateway's own words, verbatim (§15).
            return SendResult(
                outcome=SendOutcome.PERMANENT,
                error=exc.message,
                disable_destination=False,
                permanent_reason=PermanentReason.REJECTED,
            )
        except GatewayOutcomeUnknownError as exc:
            # The message may already have landed. PERMANENT is not a claim
            # that it failed — it is the only outcome that neither retries
            # (which risks a duplicate the recruiter cannot un-send, §15) nor
            # leaves the row stuck: TRANSIENT would retry, and raising would
            # strand the claim in `sending`. The destination stays enabled
            # because nothing here says the number is bad.
            log.warning("wa_linked_send_outcome_unknown", error=str(exc))
            return SendResult(
                outcome=SendOutcome.PERMANENT,
                error=f"unknown outcome, not retried to avoid a duplicate: {exc}",
                disable_destination=False,
                permanent_reason=PermanentReason.UNKNOWN,
            )
        except GatewayUnreachableError as exc:
            # Certain to have dispatched nothing, so a retry is free of the
            # duplicate risk above.
            return SendResult(outcome=SendOutcome.TRANSIENT, error=str(exc))

        if outcome.ok:
            return SendResult(
                outcome=SendOutcome.SENT,
                provider_message_id=outcome.provider_message_id,
            )

        if outcome.session_status in _DEAD_SESSION_STATUSES:
            return SendResult(
                outcome=SendOutcome.PERMANENT,
                error=(
                    "The linked WhatsApp device is no longer connected "
                    f"({outcome.session_status}). Re-pair it in Settings."
                ),
            )
        return SendResult(
            outcome=SendOutcome.TRANSIENT,
            error=f"the linked WhatsApp device is {outcome.session_status}",
        )
