"""What the ingestion pipeline did, in a form a recruiter can read.

Everything the workers do today is visible only as a structured log line on
stdout, which nobody signing in to the dashboard can reach. That makes the two
states that matter indistinguishable from each other: a mailbox that is quiet
because no vacancies arrived, and a mailbox that is quiet because the grant
died three days ago. This table is the difference — it is the source for
"Recent sync activity".

It is an *append-only audit of events*, not a status column. A status says what
is true now; the panel has to answer "when did it last work, and what broke",
which only an ordered history can.

Bounded by construction: `record_sync_event` trims each mailbox to the newest
`SYNC_ACTIVITY_KEEP_PER_MAILBOX` rows in the same transaction that inserts. A
time-based purge would need a scheduler that a mailbox syncing every ten
minutes could outrun; a per-mailbox cap cannot be outrun, because the bound is
enforced by the writer rather than by something chasing it.
"""

import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

# The vocabulary of `kind`. Constants rather than free strings at the call
# sites: the dashboard groups and labels on this value, so a typo in one worker
# would show up as a category of its own rather than as an error.
KIND_BACKFILL = "backfill"
KIND_DELTA_SYNC = "delta_sync"
KIND_SUBSCRIPTION_RENEWED = "subscription_renewed"
KIND_SUBSCRIPTION_RECREATED = "subscription_recreated"
KIND_MAILBOX_REAUTH = "mailbox_reauth_required"

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"


class SyncEvent(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "sync_events"

    # Nullable, and ON DELETE CASCADE when it is set: an event describes work
    # on one mailbox, but disconnecting a mailbox should not leave orphan rows
    # the panel cannot attribute to anything.
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("mailboxes.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    # One human-readable line, counts included where the job has them. Text
    # rather than a JSON payload because the panel renders it verbatim; a
    # structured blob would push the phrasing into the frontend, where the
    # workers' knowledge of what actually happened is not available.
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        # The panel's only query: this tenant's events, newest first. Without
        # the descending index it is a full scan of the tenant's history on
        # every dashboard load.
        Index("ix_sync_events_tenant_created", "tenant_id", "created_at"),
    )
