"""One grant of sight on one job order.

Sharing never copies. A forked opportunity would make the same vacancy exist
twice, and every count, dedup and "who filled it" answer would become
ambiguous — so a share is a row that says who may see the canonical one.

There is no `access` column. Every share is read: exactly one person, the
assignee, can edit a job order, which leaves no permission lattice to reason
about.
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


class OpportunityShare(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "opportunity_shares"

    SCOPE_USER = "user"
    SCOPE_TENANT = "tenant"
    SCOPES = (SCOPE_USER, SCOPE_TENANT)

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
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
            "scope IN ('user', 'tenant')", name="ck_opportunity_shares_scope_known"
        ),
        # The pairing rule. Same idiom as
        # `ck_opportunities_sex_requirement_has_reason`.
        CheckConstraint(
            "(scope = 'tenant') = (shared_with_user_id IS NULL)",
            name="ck_opportunity_shares_scope_target",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.id"],
            name="fk_opportunity_shares_opportunity_same_tenant",
            ondelete="CASCADE",
        ),
        # CASCADE: a share to a deleted user is meaningless, and SET NULL
        # would both convert it into a tenant broadcast and violate
        # ck_opportunity_shares_scope_target — making the user DELETE fail.
        ForeignKeyConstraint(
            ["tenant_id", "shared_with_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_opportunity_shares_recipient_same_tenant",
            ondelete="CASCADE",
        ),
        # SET NULL, for the opposite reason: the fact that someone shared this
        # must outlive the account that did.
        #
        # The column list is not optional. A bare `SET NULL` on a COMPOSITE
        # key nulls EVERY referencing column, `tenant_id` included — and
        # `tenant_id` is NOT NULL, so deleting the sharer would fail outright
        # rather than merely doing the wrong thing. Proven against the real
        # database while building this feature; `clients.assigned_user_id`
        # shipped with the bare form and had to be corrected in 19d5bbf.
        ForeignKeyConstraint(
            ["tenant_id", "shared_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_opportunity_shares_sharer_same_tenant",
            ondelete="SET NULL (shared_by_user_id)",
        ),
        # Re-sharing updates rather than duplicating. Two partial indexes
        # rather than one constraint, because the two scopes have different
        # uniqueness: one row per recipient, and one broadcast per job order.
        Index(
            "uq_opportunity_shares_per_user",
            "tenant_id",
            "opportunity_id",
            "shared_with_user_id",
            unique=True,
            postgresql_where=text("scope = 'user'"),
        ),
        Index(
            "uq_opportunity_shares_per_tenant",
            "tenant_id",
            "opportunity_id",
            unique=True,
            postgresql_where=text("scope = 'tenant'"),
        ),
    )
