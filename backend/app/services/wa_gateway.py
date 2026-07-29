"""HTTP client to the `gateway` Koyeb service (spec 2026-07-29-baileys-gateway-plan.md, §4).

Follows the idiom in `app/services/notify/channels/telegram.py`: one client
per call, `httpx.HTTPError` caught rather than left to propagate, closed in
`finally`. The one addition here is `GatewayUnreachableError` — where Telegram
answers a dead provider with a `SendResult`, the WA gateway route answers with
an API-only status the UI can render (`gateway_unreachable`, never written to
`wa_sessions` — see `app/api/wa_gateway.py`), because CLAUDE.md's rule is
explicit: **the gateway being down must not 500 the API.**

The shared secret and the gateway URL never appear in anything this module
raises or returns to a caller outside this process — see `_safe_detail`.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


class GatewayUnreachableError(Exception):
    """We never got a request out: the connection was refused or timed out
    while still being established.

    The distinction from `GatewayOutcomeUnknownError` below is not pedantry —
    it decides whether a `candidate_activities` row is written at all. Nothing
    was dispatched here, so nothing is recorded.

    Deliberately carries no URL or secret — see the module docstring — so a
    caller that logs or serialises this exception cannot leak either.
    """


class GatewayOutcomeUnknownError(Exception):
    """The request went out and we never learned what became of it.

    A read timeout, a connection dropped mid-flight, or a 5xx from the gateway
    after it had already been handed the message: in every one of these the
    send may have reached WhatsApp. §15 forbids calling that a failure, so the
    caller records `unknown` and says so to the recruiter rather than inviting
    a duplicate send.

    Same no-URL, no-secret rule as above.
    """


class GatewayRefusedError(Exception):
    """The gateway attempted the send on a live socket and WhatsApp refused it.

    `message` is the gateway's own wording, carried verbatim into
    `candidate_activities.error` (a blocked number, a bad JID, a rate limit).
    This is the *only* exception here that means a real, observed failure.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class SessionSnapshot:
    """Mirrors the gateway's response shape exactly (plan §5, §6)."""

    status: str
    qr: str | None = None
    expires_at: str | None = None
    phone_number: str | None = None
    connected_at: str | None = None


@dataclass(frozen=True)
class SendOutcome:
    """What the gateway said about one send attempt.

    `ok=True` means the gateway's socket accepted the message and WhatsApp
    returned `provider_message_id` — not delivered, and never read (§15, plan
    §8). `ok=False` carries the status the session is in instead.
    """

    ok: bool
    session_status: str
    provider_message_id: str | None = None


class WaGatewayClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def pair(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        """Start (or resume) pairing. Idempotent on the gateway side: a call
        while `connected` returns the current state without restarting the
        socket, and a call while `pairing` returns the existing QR rather than
        spawning a second one — so two browser tabs see the same code."""
        params = {"tenantId": tenant_id, "userId": user_id}
        body = await self._call("POST", "/sessions/pair", params)
        return _snapshot(body)

    async def status(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        params = {"tenantId": tenant_id, "userId": user_id}
        body = await self._call("GET", "/sessions/status", params, method_is_get=True)
        return _snapshot(body)

    async def disconnect(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        body = await self._call(
            "POST", "/sessions/disconnect", {"tenantId": tenant_id, "userId": user_id}
        )
        return _snapshot(body)

    async def send(
        self, tenant_id: str, user_id: str, *, to: str, text: str
    ) -> "SendOutcome":
        """One recipient, one text message (plan §7).

        **There is no bulk form of this method and there must not be one.**
        Plan §9 makes the API shape itself the day-one ban-risk mitigation:
        one candidate, one click, one message, so a blast campaign has nothing
        to call. `to` is derived from the candidate row server-side, never
        from the browser.

        A gateway 409 — the session is not `connected` — is an ordinary
        answer, not an error: it comes back as `ok=False` carrying the status
        the session is actually in, because "still reconnecting" and "you were
        logged out" need different sentences in the UI. Nothing was dispatched
        in that case, and the caller writes no row.

        A 422 is the opposite: the gateway did try, on a live socket, and
        WhatsApp refused. That becomes `GatewayRefusedError` carrying the
        gateway's own words, which the caller stores verbatim.

        Anything that leaves the outcome genuinely unknown — a read timeout, a
        dropped connection, a 5xx — raises `GatewayOutcomeUnknownError`, never
        a failure. This is the one call where `unknown_on_partial` is set,
        because it is the one call that changes the world: a `pair` or
        `status` request that dies mid-flight can simply be asked again.
        """
        response = await self._request(
            "POST",
            "/send",
            {"tenantId": tenant_id, "userId": user_id, "to": to, "text": text},
            unknown_on_partial=True,
        )
        if response.status_code == 409:
            body = response.json()
            return SendOutcome(ok=False, session_status=str(body.get("status", "disconnected")))
        if response.status_code == 422:
            raise GatewayRefusedError(_refusal_text(response))
        if response.status_code >= 400:
            # A 4xx we do not have a meaning for. It reached the gateway, so
            # we cannot say the message was not attempted, and we did not get
            # a refusal we could quote — which is the definition of unknown.
            log.warning("wa_gateway_send_unhandled_status", status_code=response.status_code)
            raise GatewayOutcomeUnknownError("the WA gateway gave an answer we do not understand")
        body = response.json()
        return SendOutcome(
            ok=True,
            session_status="connected",
            provider_message_id=body.get("providerMessageId"),
        )

    async def _call(
        self,
        method: str,
        path: str,
        params_or_body: dict[str, Any],
        *,
        method_is_get: bool = False,
    ) -> dict[str, Any]:
        response = await self._request(
            method, path, params_or_body, method_is_get=method_is_get
        )
        response.raise_for_status()
        return response.json()

    async def _request(
        self,
        method: str,
        path: str,
        params_or_body: dict[str, Any],
        *,
        method_is_get: bool = False,
        unknown_on_partial: bool = False,
    ) -> httpx.Response:
        """The transport, minus any opinion about which status codes are errors.

        Split out of `_call` so `send` can treat a 409 as data rather than an
        exception, while keeping one place that knows the URL, the header and
        the never-leak rule.

        `unknown_on_partial` says this call changes the world, so a transport
        error that happened *after* the bytes went out must not be reported as
        "nothing happened". Only connection-establishment failures are certain
        to have dispatched nothing; everything else is genuinely unknown. Left
        off for the read-only calls, where a retry costs nothing and `unknown`
        would be a distinction with no consequence.
        """
        client = self._client or httpx.AsyncClient(timeout=settings.WA_GATEWAY_TIMEOUT_SECONDS)
        headers = {"Authorization": f"Bearer {settings.WA_GATEWAY_SHARED_SECRET}"}
        url = f"{settings.WA_GATEWAY_URL}{path}"
        try:
            if method_is_get:
                response = await client.get(url, params=params_or_body, headers=headers)
            else:
                response = await client.post(url, json=params_or_body, headers=headers)
        except httpx.HTTPError as exc:
            # Never a URL, never the secret — see module docstring.
            dispatched = unknown_on_partial and not isinstance(exc, _NEVER_DISPATCHED)
            log.warning(
                "wa_gateway_transport_error",
                path=path,
                error=type(exc).__name__,
                dispatched=dispatched,
            )
            if dispatched:
                raise GatewayOutcomeUnknownError(
                    "the WA gateway stopped answering after the message went out"
                ) from exc
            raise GatewayUnreachableError("the WA gateway could not be reached") from exc
        finally:
            if self._owns_client:
                try:
                    await client.aclose()
                except httpx.HTTPError:
                    pass

        if response.status_code >= 500:
            log.warning("wa_gateway_5xx", path=path, status_code=response.status_code)
            if unknown_on_partial:
                # The gateway had the message before it broke. Whether it got
                # as far as WhatsApp is exactly what we do not know.
                raise GatewayOutcomeUnknownError("the WA gateway returned a server error")
            raise GatewayUnreachableError("the WA gateway returned a server error")
        return response


# The httpx errors that are certain to have dispatched nothing: the TCP
# connection or the proxy hop never completed, so no request bytes reached the
# gateway. Everything else httpx can raise (a read timeout, a half-closed
# connection, a protocol error) happens with the request already in flight.
# Listing the *safe* cases and treating the rest as unknown is deliberate: a
# transport error httpx adds tomorrow should default to "we do not know", not
# to a confident claim that nothing was sent.
_NEVER_DISPATCHED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)

# Anything longer is a gateway bug or a hostile payload, not a reason; and the
# column is read back into a browser.
_MAX_REFUSAL_CHARS = 500


def _refusal_text(response: httpx.Response) -> str:
    """The gateway's `error` string, verbatim apart from a length bound.

    No paraphrase, no mapping to one of our own sentences: §15 means a stored
    reason is the reason we were actually given. If the gateway sent something
    unparseable we say that, rather than inventing a plausible cause.
    """
    try:
        body = response.json()
    except ValueError:
        return "The WhatsApp service refused the message without saying why."
    message = body.get("error") if isinstance(body, dict) else None
    if not isinstance(message, str) or not message.strip():
        return "The WhatsApp service refused the message without saying why."
    return message.strip()[:_MAX_REFUSAL_CHARS]


def _snapshot(body: dict[str, Any]) -> SessionSnapshot:
    return SessionSnapshot(
        status=body["status"],
        qr=body.get("qr"),
        expires_at=body.get("expiresAt") or body.get("expires_at"),
        phone_number=body.get("phoneNumber") or body.get("phone_number"),
        connected_at=body.get("connectedAt") or body.get("connected_at"),
    )
