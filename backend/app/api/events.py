"""Server-sent events for the dashboard (`GET /api/events`).

The dashboard could not show new data without a manual reload, and polling was
rejected as wasteful. This is the other half: the backend already knows the
moment a row lands, so it pushes a nudge and the browser refetches through the
endpoints it already uses.

SSE rather than a WebSocket because the traffic is one-way and the browser gets
reconnection for free — `EventSource` retries on its own, which matters on a
platform that will occasionally recycle an instance underneath an open stream.

Three properties are load-bearing:

**The tenant comes from the session cookie, never from the request.** A tenant
id or channel name accepted as a parameter would be a direct isolation hole
(§18) — anyone with an account could name someone else's tenant and watch it.
`EventSource` cannot set headers, but it does send cookies, so the existing
session dependency is exactly the right authentication and no second path is
introduced.

**No database session is held open for the life of the stream.** Auth resolves
the tenant from a signed cookie and touches no database at all; nothing here
opens a session afterwards. A long-lived request that pinned a pooled
connection per connected recruiter would exhaust the pool with a handful of
dashboards open, taking down the API for everyone — a far worse outcome than
the stale screen this feature exists to fix.

**Heartbeats.** Koyeb's proxy reaps a connection that goes quiet, and nothing
in the protocol distinguishes "idle" from "dead". A comment frame on a short
interval keeps the connection alive and, as a side effect, is how the server
notices a browser that vanished without closing: the write fails.
"""

import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.auth import _require_session
from app.core.logging import get_logger
from app.services import events as events_service

log = get_logger(__name__)
router = APIRouter(tags=["events"])

# A comment frame: clients must ignore it, so it keeps the connection warm
# without the dashboard needing to know it exists.
_HEARTBEAT = ": ping\n\n"


async def _stream(request: Request, tenant_id: uuid.UUID) -> AsyncGenerator[str, None]:
    """Frames for one connected dashboard, until it goes away."""
    # Sent immediately so the browser's `onopen` fires before the first real
    # event. Without it a proxy may buffer the response headers waiting for
    # body bytes, and the dashboard would sit in "connecting" for as long as
    # the mailbox stayed quiet.
    yield _HEARTBEAT

    try:
        async for payload in events_service.subscribe(tenant_id):
            if await request.is_disconnected():
                # Redis pub/sub gives no delivery pressure to notice a dead
                # client by, so the check is explicit. Leaving the generator
                # here runs its `finally` and releases the subscription.
                break
            yield f"data: {payload}\n\n" if payload else _HEARTBEAT
    except Exception:
        # A stream that dies mid-response cannot report an error to the client
        # — the status line went out long ago. Ending the body lets EventSource
        # reconnect, which is the recovery the browser already implements.
        log.warning("event_stream_failed", tenant_id=str(tenant_id), exc_info=True)


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """Subscribe to this session's tenant. 401 when not signed in.

    The 401 is deliberately a plain error response rather than a stream that
    says "unauthorised" — `EventSource` treats a non-2xx as a failure and stops
    retrying, whereas a 200 carrying an error frame would have it reconnect
    forever against a session that is not coming back.
    """
    _user_id, tenant_id = _require_session(request)
    return StreamingResponse(
        _stream(request, tenant_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Belt and braces. There is no nginx in front of this service
            # today, but the API was behind one before and buffering there
            # holds every frame until the response ends — which, for a stream,
            # is never.
            "X-Accel-Buffering": "no",
        },
    )
