"""Microsoft Graph change-notification endpoints (plan §7, §8).

Routes are declared unprefixed; `app/main.py` mounts them under `/api`.

This is the only unauthenticated endpoint in the system, reachable by anyone
who learns the URL. Two rules govern everything here:

1. **Answer fast.** Graph allows roughly three seconds before treating the
   notification as failed. So this does one insert and one enqueue per message
   and nothing else — no Graph fetch, no model call.
2. **Answer 2xx.** Graph retries non-2xx responses for hours. A notification we
   cannot act on — forged `clientState`, a subscription we deleted, malformed
   JSON — is accepted and dropped, because retrying it would never start
   working. The only thing a 5xx here would buy is a retry storm.

Both rules point the same way: nothing an untrusted caller sends should change
how long the handler runs or what it discloses.
"""

import hmac
import uuid

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.db.session import SessionLocal
from app.services.events import KIND_MAIL, publish
from app.services.ingest.intake import record_notification
from app.workers.queue import enqueue

log = get_logger(__name__)

router = APIRouter(prefix="/graph", tags=["graph"])

# Which job resumes each lifecycle event (plan §8).
_LIFECYCLE_JOBS = {
    "reauthorizationRequired": "reauthorize_subscription",
    "subscriptionRemoved": "recreate_subscription",
    # Notifications were dropped on Graph's side. Reconcile now rather than
    # waiting for the scheduled sweep.
    "missed": "delta_sync_mailbox",
}

_RESOLVE = text("SELECT * FROM resolve_subscription(:subscription_id)")

# Read under the resolved tenant's own session — the resolver bypasses RLS and
# is kept to its three routing columns on purpose, so the pause flag is a
# second, tenant-scoped read rather than a widening of the one pre-tenant path.
_INGEST_PAUSED = text(
    "SELECT ingest_paused_at FROM mailboxes WHERE id = :mailbox_id"
)


def _validation_response(request: Request) -> Response | None:
    """Graph's subscription handshake: echo the token verbatim, as text.

    Returning JSON fails the handshake, so the token goes straight into the
    body rather than being parsed and re-encoded. It is also answered *before*
    any lookup — Graph sends it while the subscription is still being created,
    so requiring an existing row would make every creation fail.

    Reflecting caller-supplied bytes is safe only because the content type is
    text/plain; served as HTML this would be a cross-site scripting hole.
    """
    token = request.query_params.get("validationToken")
    if token is None:
        return None
    return Response(
        content=token,
        media_type="text/plain",
        status_code=200,
        # The body is caller-supplied. text/plain already makes it inert, but
        # nosniff stops a browser deciding otherwise on its own.
        headers={"X-Content-Type-Options": "nosniff"},
    )


async def _resolve(subscription_id: str):
    """Map a subscription id to its tenant, with no tenant context available.

    Uses the SECURITY DEFINER resolver, the only pre-tenant read path in the
    system: one argument in, three routing columns out. Retired subscriptions
    do not resolve, so a notification for one we replaced is dropped.
    """
    async with SessionLocal() as session:
        return (
            await session.execute(_RESOLVE, {"subscription_id": subscription_id})
        ).one_or_none()


async def _authorised(items: list[dict]):
    """Yield `(item, record)` for items whose clientState checks out.

    Resolution is memoised per request. A batch names the same subscription
    over and over, and this endpoint is unauthenticated: one database round
    trip per element would let a single request hold a worker for as long as
    the caller cared to make the list.
    """
    seen: dict[str, object] = {}
    for item in items:
        subscription_id = item.get("subscriptionId") or ""
        if subscription_id not in seen:
            seen[subscription_id] = await _resolve(subscription_id)
        record = seen[subscription_id]

        if record is None:
            log.warning("notification_unknown_subscription", subscription=subscription_id)
            continue
        if not _client_state_matches(item.get("clientState"), record.client_state):
            log.warning("notification_bad_client_state", subscription=subscription_id)
            continue
        yield item, record


def _client_state_matches(offered: str | None, expected: str | None) -> bool:
    """Constant-time comparison of the shared secret.

    This is the only thing standing between a public URL and a forged payload,
    so two details matter beyond the obvious.

    The values are encoded to bytes first. `compare_digest` raises TypeError
    when given a `str` containing non-ASCII, so a caller sending a single
    accented character would take down an endpoint whose entire contract is to
    answer 2xx — and Graph would then retry the storm.

    An empty stored secret never matches. Nothing writes one today, but if a
    subscription row ever carried one, `offered or ""` would compare equal and
    authorise anonymous inserts.
    """
    if not expected:
        return False
    return hmac.compare_digest((offered or "").encode(), expected.encode())


async def _intake_paused(tenant_id: uuid.UUID, mailbox_id: uuid.UUID) -> bool:
    """Has this mailbox's owner switched intake off?

    The webhook is the primary intake path and it never consults
    `active_mailboxes()`, so without this check every vacation email would be
    ingested in real time through a gate that was never there — the
    subscription deliberately stays alive during a pause so resume does not
    have to recreate it. Dropping here, before `record_notification`, keeps a
    long pause from accumulating rows nothing will ever fetch; the job-level
    gate in `fetch_email` remains the authoritative check behind it.
    """
    async with tenant_session(tenant_id) as session:
        paused = (
            await session.execute(_INGEST_PAUSED, {"mailbox_id": mailbox_id})
        ).scalar_one_or_none()
    return paused is not None


async def _notifications(request: Request) -> list[dict]:
    """The `value` list, bounded.

    Malformed JSON yields nothing rather than raising: a 4xx or 5xx would have
    Graph retry a body that will never parse. The cap bounds the work an
    anonymous caller can demand — Graph's own batches are far smaller.
    """
    try:
        payload = await request.json()
    except Exception:
        log.warning("notification_unparseable_body")
        return []

    items = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []

    cap = settings.GRAPH_MAX_NOTIFICATIONS_PER_REQUEST
    if len(items) > cap:
        log.warning("notification_batch_truncated", received=len(items), cap=cap)
        return items[:cap]
    return items


@router.post("/notifications")
async def notifications(request: Request) -> Response:
    if (validation := _validation_response(request)) is not None:
        return validation

    # Tenants Graph told us about in this batch, and those `record_notification`
    # already nudged. Graph only sends a notification because something moved,
    # so a batch that recorded nothing new is still the most reliable signal the
    # api process gets — the row it duplicates may have been written by the
    # delta sweep in another process, whose nudge we cannot order against this
    # request. The set difference is what keeps a single arrival from waking the
    # dashboard twice, which would double every refetch on a busy mailbox.
    tenants_seen: set[uuid.UUID] = set()
    tenants_nudged: set[uuid.UUID] = set()
    # Memoised like `_authorised`'s resolver, and for the same reason: a batch
    # names the same mailbox over and over, and this endpoint is public.
    paused_seen: dict[uuid.UUID, bool] = {}

    async for item, record in _authorised(await _notifications(request)):
        message_id = (item.get("resourceData") or {}).get("id")
        if not message_id:
            continue

        if record.mailbox_id not in paused_seen:
            paused_seen[record.mailbox_id] = await _intake_paused(
                record.tenant_id, record.mailbox_id
            )
        if paused_seen[record.mailbox_id]:
            # Deliberately not added to `tenants_seen` either: a paused
            # mailbox's arrivals must not nudge the dashboard into refetching
            # figures the pause guarantees have not changed.
            log.info("notification_dropped_intake_paused", mailbox_id=str(record.mailbox_id))
            continue

        tenants_seen.add(record.tenant_id)
        row_id = await record_notification(
            record.tenant_id, record.mailbox_id, message_id
        )
        if row_id is not None:
            tenants_nudged.add(record.tenant_id)
        if row_id is None:
            # Already known. Replay is free by design, and re-queueing would
            # only duplicate work the pipeline already has in hand.
            continue
        # The tenant travels with the job: background work has no session
        # tenant, and looking it up would need a second RLS-bypassing function.
        # A mismatched pair simply reads no row under the tenant policy.
        await enqueue(
            "fetch_email",
            email_message_id=str(row_id),
            tenant_id=str(record.tenant_id),
            mailbox_id=str(record.mailbox_id),
        )

    for tenant_id in tenants_seen - tenants_nudged:
        await publish(tenant_id, KIND_MAIL)

    return Response(status_code=202)


@router.post("/lifecycle")
async def lifecycle(request: Request) -> Response:
    """Subscription health events (plan §8).

    Without this endpoint a revoked grant surfaces only as notifications
    quietly stopping, which is indistinguishable from a slow week.
    """
    if (validation := _validation_response(request)) is not None:
        return validation

    async for item, record in _authorised(await _notifications(request)):
        event = item.get("lifecycleEvent")
        job = _LIFECYCLE_JOBS.get(event)
        if job is None:
            # Not `event=`: structlog names its own first argument that, and
            # the collision raises TypeError inside the logger.
            log.warning("lifecycle_unknown_event", lifecycle_event=event)
            continue
        # Every job carries its tenant, for the same reason `fetch_email` does:
        # background work has no session tenant, and the alternative is another
        # function that bypasses RLS to look one up.
        if job == "reauthorize_subscription":
            await enqueue(
                job,
                subscription_id=item.get("subscriptionId"),
                tenant_id=str(record.tenant_id),
                mailbox_id=str(record.mailbox_id),
            )
        else:
            await enqueue(
                job,
                tenant_id=str(record.tenant_id),
                mailbox_id=str(record.mailbox_id),
            )

    return Response(status_code=202)
