"""A recruiter asking to be shown a candidate a colleague holds.

This exists because a notification is not a record. A request that lives only
as a notification cannot be answered twice, cannot be listed, and cannot be
shown as pending — so the requester goes on believing it is open and asks
again.

Granting a request creates a `candidate_shares` row. The share is the grant;
this table is the record of how it came about.
"""

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class CandidateAccessRequest(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "candidate_access_requests"

    STATUS_PENDING = "pending"
    STATUS_GRANTED = "granted"
    STATUS_DECLINED = "declined"
    STATUSES = (STATUS_PENDING, STATUS_GRANTED, STATUS_DECLINED)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    note: Mapped[str | None] = mapped_column(Text)

    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'granted', 'declined')",
            name="ck_candidate_access_requests_status_known",
        ),
        # A resolution has a time, or the request is still open. Same paired-
        # nullability idiom as ck_candidate_shares_scope_target.
        CheckConstraint(
            "(status = 'pending') = (resolved_at IS NULL)",
            name="ck_candidate_access_requests_resolution_paired",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_access_requests_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "requested_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_access_requests_asker_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "resolved_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_access_requests_resolver_same_tenant",
            ondelete="SET NULL (resolved_by_user_id)",
        ),
        # One open request at a time. Resolved rows are not covered, so the
        # same person may ask again after a decline — circumstances change.
        Index(
            "uq_candidate_access_requests_one_pending",
            "tenant_id",
            "candidate_id",
            "requested_by_user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
