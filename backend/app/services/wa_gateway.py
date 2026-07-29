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
    """The gateway timed out, refused the connection, or returned 5xx.

    Deliberately carries no URL or secret — see the module docstring — so a
    caller that logs or serialises this exception cannot leak either.
    """


@dataclass(frozen=True)
class SessionSnapshot:
    """Mirrors the gateway's response shape exactly (plan §5, §6)."""

    status: str
    qr: str | None = None
    expires_at: str | None = None
    phone_number: str | None = None
    connected_at: str | None = None


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

    async def _call(
        self,
        method: str,
        path: str,
        params_or_body: dict[str, Any],
        *,
        method_is_get: bool = False,
    ) -> dict[str, Any]:
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
            log.warning("wa_gateway_unreachable", path=path, error=type(exc).__name__)
            raise GatewayUnreachableError("the WA gateway could not be reached") from exc
        finally:
            if self._owns_client:
                try:
                    await client.aclose()
                except httpx.HTTPError:
                    pass

        if response.status_code >= 500:
            log.warning("wa_gateway_5xx", path=path, status_code=response.status_code)
            raise GatewayUnreachableError("the WA gateway returned a server error")
        response.raise_for_status()
        return response.json()


def _snapshot(body: dict[str, Any]) -> SessionSnapshot:
    return SessionSnapshot(
        status=body["status"],
        qr=body.get("qr"),
        expires_at=body.get("expiresAt") or body.get("expires_at"),
        phone_number=body.get("phoneNumber") or body.get("phone_number"),
        connected_at=body.get("connectedAt") or body.get("connected_at"),
    )
