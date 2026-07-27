"""arq jobs (plan §7, §10).

Fetch, classify and extract are separate jobs so their failure domains stay
separate: a Graph throttle must not cost an LLM call, and a bad model response
must not cost another Graph round trip. Each retries on its own terms.

**Every job carries its tenant in the payload.** Background work has no HTTP
request and therefore no session tenant, and the alternative — a second
`SECURITY DEFINER` function to look the tenant up — would widen the only part
of the system that bypasses RLS. Carrying it is also self-validating: a job
naming a mismatched (tenant, row) pair reads no row under the tenant policy and
quietly does nothing, which is exactly the desired outcome.

This module imports `enqueue` from `app.workers.queue`, and the arq registry
that imports both lives in `app.workers.settings` — importing either of those
from here would make the two modules mutually dependent.
"""

import uuid
from datetime import datetime
from urllib.parse import quote

from arq import Retry
from sqlalchemy import text

from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.services.graph.client import (
    GraphAuthError,
    GraphClient,
    GraphNotFound,
    GraphThrottled,
)
from app.services.ms_auth import MailboxNotAuthorised, access_token_for_mailbox
from app.services.storage.r2 import R2BodyStore, body_key
from app.workers.queue import enqueue

log = get_logger(__name__)

# Only what is stored. Pulling the whole message would cost bandwidth on every
# fetch for fields nothing reads.
MESSAGE_FIELDS = (
    "id,internetMessageId,conversationId,subject,receivedDateTime,"
    "hasAttachments,from,body,bodyPreview"
)

# allow-hardcode: SQL statements, not a phrase list.
_CLAIM = text(
    "SELECT e.graph_message_id, e.processing_status, m.ms_user_id"
    " FROM email_messages e"
    " JOIN mailboxes m ON m.id = e.mailbox_id"
    " WHERE e.id = :id AND e.mailbox_id = :mailbox_id"
)

_RECORD_FETCH = text(
    """
    UPDATE email_messages SET
        internet_message_id = :internet_message_id,
        conversation_id = :conversation_id,
        sender_name = :sender_name,
        sender_email = :sender_email,
        subject = :subject,
        received_datetime = :received_datetime,
        has_attachments = :has_attachments,
        body_html_r2_key = :html_key,
        body_r2_key = :text_key,
        processing_status = 'fetched',
        attempt_count = attempt_count + 1,
        retention_until = now() + make_interval(
            days => (SELECT retention_months * 30 FROM mailboxes WHERE id = :mailbox_id)
        )
    WHERE id = :id
    """
)

_MARK_UNFETCHABLE = text(
    "UPDATE email_messages"
    " SET processing_status = 'unfetchable', source_state = 'deleted'"
    " WHERE id = :id"
)

_MARK_NEEDS_REAUTH = text("UPDATE mailboxes SET status = 'needs_reauth' WHERE id = :id")


def body_store():
    """Indirection point, so tests can swap in the in-memory store."""
    return R2BodyStore()


async def graph_client_for_mailbox(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID
) -> GraphClient:
    return GraphClient(token=await access_token_for_mailbox(tenant_id, mailbox_id))


async def fetch_email(
    ctx, *, email_message_id: str, tenant_id: str, mailbox_id: str
) -> None:
    """Fetch one message from Graph and store its source (plan §7)."""
    tenant = uuid.UUID(tenant_id)
    mailbox = uuid.UUID(mailbox_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                _CLAIM, {"id": email_message_id, "mailbox_id": mailbox}
            )
        ).one_or_none()

    if row is None:
        # Unknown row, or a job whose tenant does not own it. RLS already
        # decided; there is nothing to do and nothing to report.
        log.info("fetch_skipped_unknown_row", email_message_id=email_message_id)
        return
    if row.processing_status != "pending":
        # `rescan_stuck` and the delta sweep may both enqueue the same row.
        # Doing the work twice is waste; doing it once is the point of this.
        log.info(
            "fetch_skipped_not_pending",
            email_message_id=email_message_id,
            status=row.processing_status,
        )
        return

    try:
        client = await graph_client_for_mailbox(tenant, mailbox)
    except MailboxNotAuthorised as exc:
        await _needs_reauth(tenant, mailbox, str(exc))
        return

    try:
        message = await client.get(
            _message_path(row.ms_user_id, row.graph_message_id),
            params={"$select": MESSAGE_FIELDS},
        )
    except GraphNotFound:
        # Gone before we ever saw the body. That source really is lost, so it
        # is recorded rather than retried into an exhausted job.
        await _unfetchable(tenant, email_message_id)
        return
    except GraphAuthError as exc:
        # 403 answers the same way forever. Retrying buries the cause.
        await _needs_reauth(tenant, mailbox, str(exc))
        return
    except GraphThrottled as exc:
        # arq only reschedules on `Retry`; a bare exception is a failed job and
        # the delay Graph asked for would be discarded.
        raise Retry(defer=exc.retry_after) from exc
    finally:
        await client.aclose()

    await _store(tenant, mailbox, email_message_id, row.graph_message_id, message)
    await enqueue(
        "classify_email",
        email_message_id=email_message_id,
        tenant_id=tenant_id,
        mailbox_id=mailbox_id,
    )


def _message_path(ms_user_id: str, graph_message_id: str) -> str:
    """Build the message URL with the id encoded as a single path segment.

    Graph ids are base64-derived and can contain `/` and `+`. Interpolated
    raw, a `/` would split into extra path segments and the request would 404
    on a message that exists.
    """
    return f"/users/{quote(ms_user_id, safe='')}/messages/{quote(graph_message_id, safe='')}"


async def _store(
    tenant_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    email_message_id: str,
    graph_message_id: str,
    message: dict,
) -> None:
    """Write the body, then the row. The order is the whole point.

    A crash between the two costs one repeated write on retry, because the key
    is derived and the retry lands on it. The reverse order would leave a
    `fetched` row pointing at an object that was never written, and extraction
    would read nothing and record confident emptiness — a wrong answer rather
    than a visible failure.
    """
    html = (message.get("body") or {}).get("content") or ""
    plain = message.get("bodyPreview") or ""
    html_key = body_key(tenant_id, mailbox_id, graph_message_id, "html")
    text_key = body_key(tenant_id, mailbox_id, graph_message_id, "txt")

    store = body_store()
    await store.put(html_key, html)
    await store.put(text_key, plain)

    sender = ((message.get("from") or {}).get("emailAddress")) or {}
    async with tenant_session(tenant_id) as session:
        await session.execute(
            _RECORD_FETCH,
            {
                "internet_message_id": message.get("internetMessageId"),
                "conversation_id": message.get("conversationId"),
                "sender_name": sender.get("name"),
                "sender_email": sender.get("address"),
                "subject": message.get("subject"),
                "received_datetime": _parse_datetime(message.get("receivedDateTime")),
                "has_attachments": message.get("hasAttachments"),
                "html_key": html_key,
                "text_key": text_key,
                "mailbox_id": mailbox_id,
                "id": email_message_id,
            },
        )


async def _unfetchable(tenant_id: uuid.UUID, email_message_id: str) -> None:
    async with tenant_session(tenant_id) as session:
        await session.execute(_MARK_UNFETCHABLE, {"id": email_message_id})
    log.info("fetch_source_gone", email_message_id=email_message_id)


async def _needs_reauth(tenant_id: uuid.UUID, mailbox_id: uuid.UUID, reason: str) -> None:
    """Stop ingesting and surface it, rather than retrying a dead grant.

    The row is deliberately left `pending`: once the user reconnects,
    `rescan_stuck` picks it up and the email is fetched after all.
    """
    async with tenant_session(tenant_id) as session:
        await session.execute(_MARK_NEEDS_REAUTH, {"id": mailbox_id})
    log.warning("mailbox_needs_reauth", mailbox_id=str(mailbox_id), reason=reason[:200])


def _parse_datetime(value: str | None) -> datetime | None:
    """Graph sends RFC 3339 with a `Z`, which fromisoformat rejects before 3.11
    and still renders as a naive datetime if the suffix is simply dropped."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
