"""A connected Outlook mailbox (plan §6.2, §8, §9).

One row per mailbox we ingest from. `folder_id` is always populated, including
when `scope` is `whole_inbox`: Graph's message delta is folder-scoped, so the
whole-inbox case resolves to the well-known Inbox folder at onboarding rather
than being a second mechanism. Two scopes, one code path.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Mailbox(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "mailboxes"
    __table_args__ = (
        # Connecting the same folder twice would otherwise create a second
        # mailbox row, and since both email dedup constraints are scoped by
        # mailbox_id, every message would then ingest twice. This is the one
        # duplication path the email-level constraints cannot close.
        UniqueConstraint(
            "tenant_id", "ms_user_id", "folder_id", name="uq_mailbox_tenant_user_folder"
        ),
    )

    # SET NULL, not CASCADE: deleting a departed recruiter must not delete the
    # agency's job orders with them. Tenant deletion is the only case that
    # removes rows (spec: Retention), and CASCADE here would quietly contradict
    # that.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    ms_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    folder_id: Mapped[str] = mapped_column(String(256), nullable=False)
    folder_name: Mapped[str | None] = mapped_column(String(256))
    # server_default because onboarding and the worker write these rows with
    # raw SQL, where a Python-side default never fires.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    # Delta tokens are opaque and Graph does not bound their length.
    delta_link: Mapped[str | None] = mapped_column(Text)
    initial_sync_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_months: Mapped[int] = mapped_column(Integer, nullable=False)
