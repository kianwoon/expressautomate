"""Turning a notification into a durable row (plan §10, §19).

The insert is `ON CONFLICT DO NOTHING` because every path into the pipeline is
allowed to arrive at the same message: Graph retries notifications, the delta
sweep re-walks the folder, and onboarding's backfill overlaps the subscription
window on purpose. Replay has to be free, or the recovery layer becomes a
source of duplicates rather than a cure for gaps.

Returning the new row's id — and `None` when the row already existed — is what
lets the caller enqueue exactly once per message.
"""

import uuid

from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.events import KIND_MAIL, publish

_INSERT = text(
    """
    INSERT INTO email_messages
        (id, tenant_id, mailbox_id, graph_message_id)
    VALUES (:id, :tenant, :mailbox, :graph_id)
    ON CONFLICT (tenant_id, mailbox_id, graph_message_id) DO NOTHING
    RETURNING id
    """
)


async def record_notification(
    tenant_id: uuid.UUID, mailbox_id: uuid.UUID, graph_message_id: str
) -> uuid.UUID | None:
    """Record a message as pending work.

    The status columns are left to their database defaults rather than named
    here, so there is one place that decides what a new row starts as.
    """
    async with tenant_session(tenant_id) as session:
        result = await session.execute(
            _INSERT,
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "mailbox": mailbox_id,
                "graph_id": graph_message_id,
            },
        )
        row_id = result.scalar_one_or_none()

    # Outside the `async with`, so the transaction has committed and the row is
    # visible to the refetch this nudge provokes. Inside it, a fast browser
    # could ask before the commit landed and be told there is nothing new —
    # which looks exactly like a broken feature.
    #
    # Only for a genuinely new row: every path here is allowed to replay the
    # same message (Graph retries, the delta sweep, onboarding's backfill), and
    # nudging on a replay would refetch a dashboard that already has the data.
    if row_id is not None:
        await publish(tenant_id, KIND_MAIL)
    return row_id
