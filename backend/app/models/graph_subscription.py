"""Graph change-notification subscriptions (plan §8).

The routing table. Notifications are lean — a message id and a subscription id,
nothing else — and the webhook is unauthenticated, so this is the sole path from
an incoming notification to a tenant.

It is **not** exempt from RLS. `verify_rls_enforced()` fails startup for any
table the runtime role can read without FORCE ROW LEVEL SECURITY, structurally
rather than by an allowlist, so an exempt table would not boot. The narrow
exemption is the `resolve_subscription()` SECURITY DEFINER function instead: one
argument in, three routing columns out.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class GraphSubscription(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "graph_subscriptions"

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("mailboxes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    # Generated per subscription, never shared. Graph echoes it on every
    # notification and comparing it is all that stands between a public URL and
    # a forged payload; one global secret would make every tenant's
    # notifications forgeable the moment it leaked anywhere.
    client_state: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
