"""Look before you import (plan §6.2).

Two endpoints, in the order a user meets them. `GET /mailbox/preview` says what
is in the inbox and what each import option would cost. `POST /mailbox/ingest`
acts on the answer — and is the *only* place a mailbox row, a subscription, or
a backfill comes into existence.

Consent deliberately creates none of those. Approving "read my mail" and
choosing to import ninety days of it are separate decisions, and running them
together meant the second one was made for the user by a default they never
saw.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, text

from app.api.auth import _covers_mailbox, _provision_mailbox, _require_session
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models import MicrosoftToken, User
from app.services import ms_auth
from app.services.graph.client import GraphAuthError, GraphClient, GraphError
from app.services.graph.preview import offered_windows, preview_inbox
from app.workers.queue import enqueue

log = get_logger(__name__)
router = APIRouter(tags=["mailbox"])

# allow-hardcode: a SQL statement, not a tunable.
_EXISTING_MAILBOX = text(
    "SELECT id FROM mailboxes WHERE user_id = :user_id AND folder_id = 'inbox'"
)


# The chosen start date, and whether the walk for it has finished. Read by the
# settings endpoints, which change that date rather than the mailbox itself.
_MAILBOX_LOOKBACK = text(
    "SELECT id, initial_sync_from, backfill_completed_at FROM mailboxes"
    " WHERE user_id = :user_id AND folder_id = 'inbox'"
)

# Clearing `backfill_completed_at` is not bookkeeping — it is the whole
# mechanism. `backfill_mailbox_job` returns immediately when that column is
# set, so an enqueue without this update is a no-op that logs success.
_EXTEND_LOOKBACK = text(
    "UPDATE mailboxes SET initial_sync_from = :since, backfill_completed_at = NULL"
    " WHERE id = :mailbox_id"
)


class IngestRequest(BaseModel):
    """The user's answer to "how far back?"."""

    window: str


class LookbackRequest(BaseModel):
    """The user's answer to "reach further back than that?"."""

    window: str


async def _connected_user(request: Request) -> tuple[uuid.UUID, uuid.UUID, str]:
    """(tenant, user, ms object id) for a session that may read its own mailbox.

    The scope check is repeated here rather than trusted from the dashboard: a
    client that calls this endpoint directly without the mailbox grant would
    otherwise reach Graph and get an opaque 403 back, when the real answer is
    "you have not connected a mailbox yet".
    """
    user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        user = (
            await session.execute(select(User).where(User.id == user_uuid))
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=401, detail="Not signed in.")
        scope = (
            await session.execute(
                select(MicrosoftToken.scope).where(MicrosoftToken.user_id == user.id)
            )
        ).scalar_one_or_none()

    if not _covers_mailbox(scope):
        raise HTTPException(status_code=403, detail="Connect your mailbox first.")
    if not user.ms_object_id:
        raise HTTPException(status_code=403, detail="This account has no Microsoft mailbox.")
    return tenant_uuid, user.id, user.ms_object_id


@router.get("/mailbox/preview")
async def mailbox_preview(request: Request) -> dict:
    """What is in the inbox, and what each import option would pull in."""
    tenant_uuid, user_id, ms_user_id = await _connected_user(request)

    # A missing Graph URL is an operator problem, and 503 says so — letting it
    # through produced a 500 that read as a code fault. Checked here rather
    # than in `_connected_user` for two reasons: `/mailbox/ingest` makes no
    # Graph call in this process (its work runs on the workers, which have
    # their own env), so gating it here would block a path that still works;
    # and asking after the session check keeps the deployment's configuration
    # from being readable by anyone who is not signed in.
    if not settings.graph_configured():
        log.error("graph_base_url_missing", route="mailbox_preview")
        raise HTTPException(
            status_code=503,
            detail="Mailbox access is not configured on this deployment "
            "(GRAPH_BASE_URL). See docs/setup.md.",
        )

    try:
        token = await ms_auth.access_token_for_user(tenant_uuid, user_id)
    except ms_auth.MailboxNotAuthorised as exc:
        # The grant is on file but Entra will not honour it — revoked, expired,
        # or a password change. Reconnecting is the fix, so say that instead of
        # letting a 500 read as an outage.
        log.info("mailbox_preview_unauthorised", user_id=str(user_id), error=str(exc))
        raise HTTPException(status_code=403, detail="Reconnect your mailbox.") from exc

    async with GraphClient(token) as client:
        try:
            preview = await preview_inbox(client, ms_user_id)
        except GraphAuthError as exc:
            # A grant that refreshed fine but that Graph itself refuses —
            # revoked between the refresh and the read, or an admin policy.
            # Reported as 403 like the other dead-grant case, because
            # "reconnect" is the fix; a 502 would blame our own uptime for it.
            log.info("mailbox_preview_refused", user_id=str(user_id), error=repr(exc))
            raise HTTPException(status_code=403, detail="Reconnect your mailbox.") from exc
        except (GraphError, httpx.HTTPStatusError, httpx.RequestError) as exc:
            # `RequestError` covers everything that fails before a response
            # exists — DNS, connect, timeout, a base URL with no host. None of
            # it reaches `_unwrap`, so without this arm it surfaced as a bare
            # 500 with the cause buried in a traceback. `repr` is deliberate:
            # the exception class is the whole diagnosis for this family.
            log.warning("mailbox_preview_failed", user_id=str(user_id), error=repr(exc))
            raise HTTPException(
                status_code=502, detail="Microsoft could not be reached just now."
            ) from exc

    return {
        "folder": preview.folder,
        "total": preview.total,
        "oldest_received": preview.oldest_received.isoformat() if preview.oldest_received else None,
        "options": [
            {"key": w.key, "label": w.label, "days": w.days, "emails": w.emails}
            for w in preview.windows
        ],
    }


@router.post("/mailbox/ingest")
async def start_ingestion(request: Request, body: IngestRequest) -> dict:
    """Provision the mailbox for the chosen period and start reading.

    Idempotent by way of the upsert: a double-submitted form reactivates the
    same row rather than creating a second one, and the queued jobs are the
    same ones a reconnection runs.
    """
    tenant_uuid, user_id, ms_user_id = await _connected_user(request)

    window = _resolve_window(body.window)

    async with tenant_session(tenant_uuid) as session:
        existing = (
            await session.execute(_EXISTING_MAILBOX, {"user_id": user_id})
        ).scalar_one_or_none()
        if existing is not None:
            # Already chosen. Re-answering would either re-walk history the
            # user did not ask for or silently do nothing; refusing says which.
            raise HTTPException(
                status_code=409, detail="This mailbox is already being ingested."
            )
        mailbox_id = await _provision_mailbox(session, tenant_uuid, user_id, ms_user_id, window)

    # Queued, not inline: a large mailbox must not hold the request open.
    # `recreate_subscription` also retires anything left from a previous
    # connection, so reconnecting takes the same path as connecting.
    await enqueue("recreate_subscription", tenant_id=str(tenant_uuid), mailbox_id=str(mailbox_id))
    await enqueue("backfill_mailbox_job", tenant_id=str(tenant_uuid), mailbox_id=str(mailbox_id))

    log.info(
        "mailbox_ingestion_started",
        tenant_id=str(tenant_uuid),
        mailbox_id=str(mailbox_id),
        window=body.window,
    )
    return {"mailbox_id": str(mailbox_id), "initial_sync_from": window.isoformat()}


@router.get("/mailbox/settings")
async def mailbox_settings(request: Request) -> dict:
    """What "how far back" is set to now, and what it could be changed to.

    Deliberately not behind `_connected_user`: this reads one of our own rows
    and offers to rewrite it. Nothing here touches Graph, and refusing someone
    whose grant has since lapsed would lock them out of the one screen that
    lets them widen the window they will want widened when they reconnect.
    """
    user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        row = (
            await session.execute(_MAILBOX_LOOKBACK, {"user_id": user_uuid})
        ).one_or_none()

    if row is None or row.initial_sync_from is None:
        # No period has been chosen yet, so there is nothing to change. The
        # dashboard's onboarding step is where that decision is made; sending
        # the user here to make it a second way would give one setting two
        # owners.
        raise HTTPException(
            status_code=404, detail="No mailbox is being read yet."
        )

    current = row.initial_sync_from
    return {
        "initial_sync_from": current.isoformat(),
        "backfill_complete": row.backfill_completed_at is not None,
        # Only the options that reach further back than the current setting.
        # Offering a shorter one would be a lie: moving the date later
        # un-imports nothing, so the control would read as a delete and behave
        # as a no-op.
        "options": [
            {"key": key, "label": label, "days": days}
            for key, label, days in offered_windows()
            if _extends(_window_start(days), current)
        ],
    }


@router.post("/mailbox/settings/lookback")
async def extend_lookback(request: Request, body: LookbackRequest) -> dict:
    """Reach further back through the inbox, and read what that adds.

    One direction only. The re-walk overlaps everything already held, which is
    free: `record_notification` inserts `ON CONFLICT DO NOTHING` on
    (tenant_id, mailbox_id, graph_message_id) and returns None for a row that
    existed, so a known message is neither stored twice nor re-enqueued for
    extraction — and `_walk` does not count it against the message cap either,
    so the extension's budget is spent on the mail it came for.
    """
    user_uuid, tenant_uuid = _require_session(request)
    since = _resolve_window(body.window)

    async with tenant_session(tenant_uuid) as session:
        row = (
            await session.execute(_MAILBOX_LOOKBACK, {"user_id": user_uuid})
        ).one_or_none()
        if row is None or row.initial_sync_from is None:
            raise HTTPException(status_code=404, detail="No mailbox is being read yet.")
        if not _extends(since, row.initial_sync_from):
            # Refused rather than quietly accepted. Writing a later date would
            # change the stored setting without removing a single email, so the
            # user would be told their history had shrunk when it had not.
            raise HTTPException(
                status_code=400,
                detail=(
                    "That period is not further back than the one you already have. "
                    "Reading more history only ever adds emails — it cannot remove "
                    "any that were already read."
                ),
            )
        await session.execute(
            _EXTEND_LOOKBACK, {"since": since, "mailbox_id": row.id}
        )
        mailbox_id = row.id

    # Enqueued after the transaction commits, not inside it: a job that starts
    # before the new start date is durable would read the old one and mark the
    # mailbox backfilled again.
    await enqueue("backfill_mailbox_job", tenant_id=str(tenant_uuid), mailbox_id=str(mailbox_id))

    log.info(
        "mailbox_lookback_extended",
        tenant_id=str(tenant_uuid),
        mailbox_id=str(mailbox_id),
        window=body.window,
        initial_sync_from=since.isoformat(),
    )
    return {"initial_sync_from": since.isoformat()}


def _window_start(days: int | None) -> datetime:
    """The start date an option would produce if chosen right now."""
    now = datetime.now(UTC)
    return now if days is None else now - timedelta(days=days)


def _extends(candidate: datetime, current: datetime) -> bool:
    """Is `candidate` far enough behind `current` to be worth a re-walk?

    A bare `<` is not enough. `current` is a fixed instant while every option
    is measured from `now`, so the gap between them widens on its own: a user
    who chose the longest window last week would be offered that same window
    again, as an "extension" worth a week of history and a full re-walk of the
    mailbox to get it. The floor is configuration because how much extra
    history justifies thousands of Graph calls is an operator's judgement.
    """
    gap = current - candidate
    return gap >= timedelta(days=settings.LOOKBACK_EXTENSION_MIN_DAYS)


def _resolve_window(key: str) -> datetime:
    """The chosen option, back into a start date.

    Validated against the same list the preview offered rather than parsing an
    arbitrary number of days out of the request: a client that asks for 3650
    days would otherwise get a backfill nothing can complete, and the cap in
    `preview` would be advisory rather than real.
    """
    for offered_key, _label, days in offered_windows():
        if offered_key == key:
            # "From now on" is a real choice, not an absence of one: watch the
            # mailbox, import no history. Recorded as now rather than NULL so
            # the backfill sweep sees a completed decision instead of an
            # unconfigured mailbox to retry forever — which is why
            # `_window_start` maps it to `now` rather than returning None.
            return _window_start(days)
    raise HTTPException(status_code=400, detail="Choose one of the offered periods.")
