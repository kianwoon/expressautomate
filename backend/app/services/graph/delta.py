"""Delta synchronisation and reconciliation (plan §9, §6.2).

The recovery path. Webhooks are fast but lossy — a missed notification, a
webhook outage, a Graph incident — and this walk is what makes any of that
survivable. It is also the backfill path: onboarding differs only in where the
walk starts.

One distinction matters more than the rest. Graph's message delta is
**folder-scoped**, so an `@removed` event usually means the recruiter filed the
mail into a subfolder rather than deleted it. The body is already safe in R2 and
the vacancy it described is still real, so a removal updates `source_state` and
leaves `processing_status` alone. Collapsing the two would quietly retire live
job orders every time someone tidied their inbox.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.services.graph.client import GraphClient, GraphResyncRequired
from app.services.ingest.intake import record_notification
from app.workers.queue import enqueue

log = get_logger(__name__)


@dataclass(frozen=True)
class DeltaResult:
    """What one walk did.

    Three numbers rather than one, because callers need different ones:
    onboarding reports `recorded` to the user, the cap is measured in
    `recorded`, and `capped` is how anything downstream knows the walk stopped
    short rather than reaching the end of the folder.
    """

    seen: int
    recorded: int
    capped: bool

# allow-hardcode: SQL statements, not a phrase list.
_MAILBOX = text(
    "SELECT ms_user_id, folder_id, delta_link FROM mailboxes WHERE id = :mailbox_id"
)

_STORE_LINK = text("UPDATE mailboxes SET delta_link = :delta_link WHERE id = :mailbox_id")

_MARK_REMOVED = text(
    # `processing_status` is deliberately untouched: what we extracted from
    # this message is still true, wherever the recruiter has since filed it.
    "UPDATE email_messages SET source_state = 'removed_from_folder'"
    " WHERE mailbox_id = :mailbox_id AND graph_message_id = :graph_message_id"
)

_MARK_BACKFILLED = text(
    "UPDATE mailboxes SET backfill_completed_at = now() WHERE id = :mailbox_id"
)


async def sync_mailbox(
    tenant_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    client: GraphClient,
    *,
    max_messages: int | None = None,
    since: datetime | None = None,
) -> DeltaResult:
    """Walk the mailbox delta, recording what it finds.

    A stored checkpoint that Graph has expired (410) is dropped and the walk
    restarts from the folder — once. Retrying beyond that would spin, and a
    checkpoint left in place would make every future sweep fail identically,
    leaving the recovery layer permanently dead while looking merely quiet.
    """
    start = await _walk_start(tenant_id, mailbox_id, since)
    # Only a checkpoint can expire. A backfill builds its own filtered URL, so
    # a 410 there is not something a restart would fix.
    resuming_a_checkpoint = since is None

    try:
        return await _walk(tenant_id, mailbox_id, client, start, max_messages)
    except GraphResyncRequired:
        if not resuming_a_checkpoint:
            raise
        log.warning("delta_token_expired", mailbox_id=str(mailbox_id))
        await _store_link(tenant_id, mailbox_id, None)
        fresh = await _walk_start(tenant_id, mailbox_id, since)
        return await _walk(tenant_id, mailbox_id, client, fresh, max_messages)


async def backfill_mailbox(
    tenant_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    client: GraphClient,
    since: datetime,
) -> DeltaResult:
    """The initial historical walk (plan §6.2), bounded by configuration.

    Stops at whichever limit comes first and marks the mailbox backfilled from
    that point — including when the cap stopped it. That is the spec's
    intent: bulk historical import is a different feature with a different
    mechanism, and leaving the mailbox permanently un-backfilled would keep
    re-running a walk that is meant to be bounded.

    The truncation is logged at warning, because "we imported the last 5000"
    is something the user should be told rather than left to infer from a
    thinner history than they asked for.
    """
    result = await sync_mailbox(
        tenant_id,
        mailbox_id,
        client,
        max_messages=settings.INITIAL_SYNC_MAX_MESSAGES,
        since=since,
    )
    async with tenant_session(tenant_id) as session:
        await session.execute(_MARK_BACKFILLED, {"mailbox_id": mailbox_id})

    if result.capped:
        log.warning(
            "backfill_truncated",
            mailbox_id=str(mailbox_id),
            recorded=result.recorded,
            cap=settings.INITIAL_SYNC_MAX_MESSAGES,
        )
    else:
        log.info(
            "backfill_complete", mailbox_id=str(mailbox_id), recorded=result.recorded
        )
    return result


async def _walk(
    tenant_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    client: GraphClient,
    url: str,
    max_messages: int | None,
) -> DeltaResult:
    seen = 0
    recorded = 0
    delta_link: str | None = None

    while url:
        page = await client.get(url)

        # The whole page is processed before the cap is consulted. Stopping
        # mid-page and resuming from `@odata.nextLink` would skip every item
        # after the cap position — silently, permanently, on the code path
        # whose entire purpose is not losing mail. Overshooting the cap by at
        # most one page is the cheaper error by a wide margin.
        for item in page.get("value", []):
            message_id = item.get("id")
            if not message_id:
                continue

            if "@removed" in item:
                await _mark_removed(tenant_id, mailbox_id, message_id)
                continue

            seen += 1
            row_id = await record_notification(tenant_id, mailbox_id, message_id)
            if row_id is None:
                # Already known — the webhook, an earlier sweep or the backfill
                # got here first. Not new work, and deliberately not counted
                # against the cap: a resumed walk would otherwise spend its
                # whole budget re-reading what it already has and never reach
                # the mail it stopped short of.
                continue

            recorded += 1
            await enqueue(
                "fetch_email",
                email_message_id=str(row_id),
                tenant_id=str(tenant_id),
                mailbox_id=str(mailbox_id),
            )

        delta_link = page.get("@odata.deltaLink") or delta_link
        next_link = page.get("@odata.nextLink")

        # `next_link` is the test, not the count: reaching the cap on the final
        # page means the walk *finished* and merely happened to land on the
        # number. Reporting that as truncated would tell the user their history
        # was cut short when nothing was left to fetch.
        if max_messages is not None and recorded >= max_messages and next_link:
            # Store where we stopped, not nothing. Without a checkpoint the
            # next sweep re-walks from the beginning and caps in the same
            # place — forever, never reaching the newest mail.
            await _store_link(tenant_id, mailbox_id, next_link)
            log.info(
                "delta_walk_capped",
                mailbox_id=str(mailbox_id),
                cap=max_messages,
                recorded=recorded,
            )
            return DeltaResult(seen=seen, recorded=recorded, capped=True)

        url = next_link

    if delta_link:
        await _store_link(tenant_id, mailbox_id, delta_link)
    return DeltaResult(seen=seen, recorded=recorded, capped=False)


async def _walk_start(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, since: datetime | None
) -> str:
    """Where this walk begins: a stored checkpoint, or the folder itself."""
    async with tenant_session(tenant_id) as session:
        row = (await session.execute(_MAILBOX, {"mailbox_id": mailbox_id})).one()

    # A backfill names its own starting point, so it never resumes a checkpoint.
    if row.delta_link and since is None:
        return row.delta_link

    base = (
        f"/users/{quote(row.ms_user_id, safe='')}"
        f"/mailFolders/{quote(row.folder_id, safe='')}/messages/delta"
    )
    if since is None:
        return base
    stamp = since.isoformat().replace("+00:00", "Z")
    return f"{base}?$filter=receivedDateTime ge {stamp}"


async def _mark_removed(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, graph_message_id: str
) -> None:
    async with tenant_session(tenant_id) as session:
        result = await session.execute(
            _MARK_REMOVED,
            {"mailbox_id": mailbox_id, "graph_message_id": graph_message_id},
        )

    if result.rowcount == 0:
        # A message leaving a folder we never recorded it entering. Harmless,
        # but it means the arrival was missed — the signal is worth having when
        # diagnosing why a job order never appeared.
        log.info(
            "delta_removal_for_unknown_message",
            mailbox_id=str(mailbox_id),
            graph_message_id=graph_message_id,
        )


async def _store_link(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, delta_link: str | None
) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(
            _STORE_LINK, {"delta_link": delta_link, "mailbox_id": mailbox_id}
        )
