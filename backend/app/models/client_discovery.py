"""One header-only scan of a recruiter's mailbox (spec 2026-08-02).

A run is an **ephemeral cache**, not a system of record — the durable output
of discovery is rows in `clients` and `client_contacts`, written by the
enrichment and create paths. What lives here is what must survive between
"the worker finished" and "the user clicked Create": the ranked new domains,
with their contacts, as JSONB.

One live row per user by construction: starting a scan deletes the user's
previous runs. That keeps the table a bounded working set rather than a
history — "the list I was shown" has no evidentiary value once acted on,
unlike a `SourcingRun`, which exists precisely to be re-read later.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class ClientDiscoveryRun(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "client_discovery_runs"

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STATES = (PENDING, RUNNING, DONE, FAILED)

    # Whose mailbox was read. The scan is strictly first-person — the grant
    # being exercised is this user's own — so results are filtered per user
    # as well as per tenant.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDING)
    # Recorded on the row rather than re-read from settings at render time, so
    # a run answers for the window it actually walked even if the deployment's
    # setting has since changed.
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # What the walk read, so "found 3 companies" can be told apart from
    # "read 3 emails" at a glance.
    inbox_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The walk stopped at CLIENT_DISCOVERY_MAX_MESSAGES with pages remaining.
    messages_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # More new domains ranked than CLIENT_DISCOVERY_MAX_DOMAINS could store.
    domains_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # The automatic backfill's receipt: how many existing clients gained at
    # least one contact, and how many contact rows were written in total.
    clients_enriched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contacts_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Ranked NEW domains only — domains already held by a client (directly or
    # through a merge chain) were enriched instead and never appear here.
    # Shape per entry: {domain, score, received, sent, unique_contacts,
    # last_activity, created, contacts: [{email, name, inbound, outbound,
    # last_activity}]}.
    results: Mapped[list | None] = mapped_column(JSONB)

    # Why a failed run failed, in words a recruiter can act on.
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Composite, so a run cannot cross agencies. CASCADE: a run is an
        # ephemeral working set and dies with its user, unlike a client.
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_client_discovery_runs_user_same_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="ck_client_discovery_runs_status",
        ),
    )
