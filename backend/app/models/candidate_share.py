"""One grant of sight on one candidate.

Sharing never copies. A forked candidate would make the same person exist
twice, and every headcount, dedup and "have we approached them before" answer
would become wrong by construction — so a share is a row that says who may see
the canonical one.

There is no `access` column. Every share is read: exactly one person, the
owner, can edit a candidate, which leaves no permission lattice to reason
about. The one thing a recipient may write is an activity row, which records
what the recipient did rather than editing the candidate.

Structurally identical to `opportunity_share.py`, deliberately — one sharing
idiom in this codebase rather than two.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class CandidateShare(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "candidate_shares"

    SCOPE_USER = "user"
    SCOPE_TENANT = "tenant"
    SCOPES = (SCOPE_USER, SCOPE_TENANT)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)

    # NULL if and only if scope='tenant' — one broadcast row rather than one
    # row per colleague, so a recruiter hired next month inherits it.
    shared_with_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), index=True
    )
    shared_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "scope IN ('user', 'tenant')", name="ck_candidate_shares_scope_known"
        ),
        CheckConstraint(
            "(scope = 'tenant') = (shared_with_user_id IS NULL)",
            name="ck_candidate_shares_scope_target",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            name="fk_candidate_shares_candidate_same_tenant",
            ondelete="CASCADE",
        ),
        # CASCADE: a share to a deleted user is meaningless, and SET NULL
        # would both convert it into a tenant broadcast and violate
        # ck_candidate_shares_scope_target — making the user DELETE fail.
        ForeignKeyConstraint(
            ["tenant_id", "shared_with_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_shares_recipient_same_tenant",
            ondelete="CASCADE",
        ),
        # SET NULL, for the opposite reason: the fact that someone shared this
        # must outlive the account that did. The column list is not optional —
        # see the note in `opportunity_share.py`.
        ForeignKeyConstraint(
            ["tenant_id", "shared_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_candidate_shares_sharer_same_tenant",
            ondelete="SET NULL (shared_by_user_id)",
        ),
        Index(
            "uq_candidate_shares_per_user",
            "tenant_id",
            "candidate_id",
            "shared_with_user_id",
            unique=True,
            postgresql_where=text("scope = 'user'"),
        ),
        Index(
            "uq_candidate_shares_per_tenant",
            "tenant_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("scope = 'tenant'"),
        ),
    )
