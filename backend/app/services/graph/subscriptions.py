"""Graph subscription lifecycle (plan §8).

Subscriptions expire and **Graph decides when**. It may grant less than we ask
for, and the documented maximum has changed more than once, so nothing here
assumes a duration: the renewal point is always derived from the
`expirationDateTime` Graph actually returned. A hardcoded "three days" is how a
subscription lapses quietly — and a lapsed subscription looks exactly like a
week with no job orders in it.

Each subscription carries its own random `clientState`. It is the only thing
standing between a public webhook URL and a forged payload, so a single shared
secret would make every tenant's notifications forgeable the moment it leaked
anywhere; per-subscription limits the blast radius to one mailbox.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.services.events import KIND_MAILBOX, publish
from app.services.graph.client import MAILBOX_ROOT, GraphClient

log = get_logger(__name__)

# Long enough that guessing is hopeless and comparison stays cheap.
_CLIENT_STATE_BYTES = 32

# allow-hardcode: SQL statements, not a phrase list.
_INSERT = text(
    """
    INSERT INTO graph_subscriptions
        (id, tenant_id, mailbox_id, subscription_id, resource, client_state,
         expires_at, status)
    VALUES (:id, :tenant_id, :mailbox_id, :subscription_id, :resource,
            :client_state, :expires_at, 'active')
    """
)

_RENEW = text(
    "UPDATE graph_subscriptions"
    " SET expires_at = :expires_at, last_renewed_at = now(), status = 'active'"
    " WHERE subscription_id = :subscription_id"
)

_RETIRE = text(
    "UPDATE graph_subscriptions SET status = :status"
    " WHERE subscription_id = :subscription_id"
)


def renewal_threshold(granted_at: datetime, expires_at: datetime) -> datetime:
    """When renewal becomes due: a fraction into the lifetime Graph granted.

    Derived rather than fixed, because the grant is the only thing that knows
    how long it lasts. A subscription Graph shortened to two hours is due in
    one, not in three days.

    `granted_at` is when the *current* lifetime started, so callers should pass
    `last_renewed_at` once a subscription has been renewed and fall back to
    `created_at` only for one that never has.

    Getting that wrong is wasteful rather than dangerous, which is worth
    stating precisely: the result is the weighted midpoint of the interval, so
    an older basis pulls the threshold *earlier*, never later. A stale
    `created_at` therefore renews too often — extra Graph calls — but cannot
    make a subscription lapse. The error is bounded in the safe direction.
    """
    return granted_at + (expires_at - granted_at) * settings.GRAPH_SUBSCRIPTION_RENEW_MARGIN


def build_subscription_payload(ms_user_id: str, folder_id: str) -> dict:
    """The creation request, including a freshly generated secret."""
    return {
        "changeType": "created",
        "notificationUrl": settings.MS_WEBHOOK_NOTIFICATION_URL,
        # Without this, a revoked grant surfaces only as notifications quietly
        # stopping — indistinguishable from a slow week.
        "lifecycleNotificationUrl": settings.MS_WEBHOOK_LIFECYCLE_URL,
        # Encoded for the same reason `_message_path` in jobs.py is: these ids
        # are opaque and may contain `/` or `?`. This one travels in a JSON
        # body rather than a URL, so a stray character would not misroute a
        # request — it would silently subscribe to something else.
        # `MAILBOX_ROOT` rather than `/users/{id}`: Graph resolves it from the
        # delegated token that creates the subscription, and `/users/{id}` is
        # rejected outright for personal accounts.
        "resource": f"{MAILBOX_ROOT}/mailFolders/{quote(folder_id, safe='')}/messages",
        "clientState": secrets.token_urlsafe(_CLIENT_STATE_BYTES),
        "expirationDateTime": _requested_expiry(),
        # The message is fetched over an authenticated channel instead. Trusting
        # a body posted to a public URL would make the webhook a data source
        # rather than a signal (plan §7).
        "includeResourceData": False,
    }


async def create_subscription(
    tenant_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    ms_user_id: str,
    folder_id: str,
    client: GraphClient,
) -> str:
    """Subscribe to a folder and record the result. Returns the Graph id."""
    payload = build_subscription_payload(ms_user_id, folder_id)
    created = await client.post("/subscriptions", json=payload)

    try:
        async with tenant_session(tenant_id) as session:
            await session.execute(
                _INSERT,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "mailbox_id": mailbox_id,
                    "subscription_id": created["id"],
                    "resource": payload["resource"],
                    # The secret Graph was told, not a fresh one — the webhook
                    # compares incoming notifications against this exact value.
                    "client_state": payload["clientState"],
                    "expires_at": _parse_expiry(created["expirationDateTime"]),
                },
            )
    except Exception:
        # Graph now has a live subscription whose secret nobody stored. Its
        # notifications would be rejected as forged until it expired days
        # later, and nothing would say why. Undo it before giving up.
        log.exception(
            "subscription_recording_failed", subscription_id=created.get("id")
        )
        await _abandon(created.get("id"), client)
        raise

    log.info(
        "subscription_created",
        mailbox_id=str(mailbox_id),
        subscription_id=created["id"],
    )
    # After the commit above, so a dashboard that refetches on this nudge reads
    # the subscription rather than the moment before it existed. This is the
    # transition from "connected" to "actually ingesting", and it is the one a
    # user is watching for during onboarding — without a push they would sit on
    # a screen saying nothing is happening while it already is.
    await publish(tenant_id, KIND_MAILBOX)
    return created["id"]


async def renew_subscription(
    tenant_id: uuid.UUID, subscription_id: str, client: GraphClient
) -> None:
    """Extend a subscription, recording the new expiry Graph granted.

    The `clientState` is deliberately untouched: the webhook compares against
    the stored value, so rotating it here without telling Graph would reject
    every notification until the next renewal.
    """
    updated = await client.patch(
        f"/subscriptions/{subscription_id}",
        json={"expirationDateTime": _requested_expiry()},
    )

    async with tenant_session(tenant_id) as session:
        await session.execute(
            _RENEW,
            {
                "expires_at": _parse_expiry(updated["expirationDateTime"]),
                "subscription_id": subscription_id,
            },
        )
    log.info("subscription_renewed", subscription_id=subscription_id)


async def delete_subscription(
    tenant_id: uuid.UUID, subscription_id: str, client: GraphClient
) -> None:
    """Drop a subscription at Graph and retire the row.

    The row is retired whether or not Graph still had it: `resolve_subscription`
    only returns active rows, so leaving it would keep routing notifications for
    a subscription that no longer exists.
    """
    existed = await client.delete(f"/subscriptions/{subscription_id}")
    if not existed:
        # Graph had already dropped it — worth noticing, because a subscription
        # vanishing on its own means notifications stopped before we asked them
        # to. The row is retired either way.
        log.warning("subscription_already_absent", subscription_id=subscription_id)

    await _retire(tenant_id, subscription_id, "deleted")


async def retire_subscription(
    tenant_id: uuid.UUID, subscription_id: str, status: str
) -> None:
    """Mark a subscription as no longer routing, without calling Graph."""
    await _retire(tenant_id, subscription_id, status)


async def _abandon(subscription_id: str | None, client: GraphClient) -> None:
    """Best-effort removal of a subscription we failed to record.

    Deliberately swallows its own failure: the caller is already raising, and
    the original error is the one worth surfacing. If this does fail, the
    subscription lapses on its own and the renewal sweep never sees it, because
    no row was ever written.
    """
    if not subscription_id:
        return
    try:
        await client.delete(f"/subscriptions/{subscription_id}")
    except Exception:
        log.exception("subscription_abandon_failed", subscription_id=subscription_id)


async def _retire(tenant_id: uuid.UUID, subscription_id: str, status: str) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            _RETIRE, {"status": status, "subscription_id": subscription_id}
        )


def _requested_expiry() -> str:
    """What to ask Graph for. It is free to grant less, which is why nothing
    downstream reads this value back."""
    expiry = datetime.now(UTC) + timedelta(
        minutes=settings.GRAPH_SUBSCRIPTION_REQUEST_MINUTES
    )
    return expiry.isoformat().replace("+00:00", "Z")


def _parse_expiry(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
