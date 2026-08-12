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

**The destination's stored address is deliberately NOT the send target.** A
self-chat always goes to the recruiter's *current* paired number
(`wa_sessions.phone_e164`), not to whatever number the destination recorded
when it was linked. The two diverge the moment a recruiter re-pairs with a
different device: the destination row keeps the old number (that is the
"first number still receives notifications" bug), so sending to the stored
address would message the old device — or, if that number has since been
reassigned, a stranger — while the recruiter watches a notification go
nowhere they own. The live session number is the only honest target, which is
why `apply_internal_status` also reconciles the destination's stored address
on every connect (see `app/api/wa_gateway.py`); this read is the belt-and-
braces that makes the send correct even for a destination whose row predates
that reconciliation.
"""

import uuid

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.wa_session import (
    STATUS_CONNECTED,
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

# The self-chat target: the recruiter's own number as the gateway last
# reported it. NULL until a pairing has ever succeeded.
_LIVE_SESSION_PHONE = text(
    "SELECT phone_e164 FROM wa_sessions WHERE user_id = :user_id AND status = :connected"
)

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

        # The live number is the send target, not the stored `address` (see the
        # module docstring): after a re-pair the destination row may still carry
        # the old number, and messaging it would reach the previous device — or
        # whoever holds that number now. The gateway's own `send()` refuses a
        # not-connected session, so if there is no current number the session is
        # effectively dead; the refusal below is the honest outcome.
        try:
            async with tenant_session(self._tenant_id) as session:
                live = (
                    await session.execute(
                        _LIVE_SESSION_PHONE,
                        {"user_id": self._user_id, "connected": STATUS_CONNECTED},
                    )
                ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 — see below
            # A database error here must not strand the caller's claimed row in
            # `sending` (the same never-raise invariant this method protects for
            # the gateway client). An unknown current number is not a fact we
            # can act on: fall back to the stored address, which at least keeps
            # the send attempt going and lets the gateway's own liveness check
            # decide. The stored address is stale only after a re-pair, and in
            # that state the session row is `connected` with a phone, so the
            # failure to read it is the exceptional case, not the stale one.
            log.exception("wa_linked_session_phone_unreadable")
            live = None

        if live is not None:
            address = live

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
