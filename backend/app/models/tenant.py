"""Tenant and User — the multi-tenancy root (plan §18)."""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Tenant(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    # Entra ID tenant this agency signed in from; null until first MS login.
    # For a personal Microsoft account this holds the shared MSA consumer GUID,
    # which is *not* the primary key — see `_tenant_for` in app/api/auth.py.
    ms_tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # True when this tenant is one private-to-one-person workspace derived from
    # a personal Microsoft account, rather than a company Entra tenant that
    # colleagues share. Onboarding and invites must treat the two differently.
    is_personal_account: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # The daily LLM-run counter (migration c1v2p0000001): how many
    # user-triggered analyses this agency has started on `llm_runs_date`.
    # Lives on the tenant — not a counter table, and not a COUNT(*) of the
    # intelligence rows, because those POSTs are upserts: a re-run updates
    # the existing row, and a COUNT() cannot count events that create no
    # rows. Incremented atomically in the POST (one UPDATE..RETURNING that
    # also performs the date rollover), read against
    # INTELLIGENCE_DAILY_QUOTA. Refused POSTs have already spent their
    # increment — harmless, refusals cost no model call and the window
    # resets at midnight UTC.
    llm_runs_date: Mapped[date | None] = mapped_column(Date)
    llm_runs_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )

    # passive_deletes defers to the FK's ON DELETE CASCADE; without it the ORM
    # would try to NULL out users.tenant_id, which the NOT NULL column rejects.
    users: Mapped[list["User"]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class User(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "users"
    # Identity is (tenant_id, ms_object_id): the Entra object id is immutable,
    # while the UPN behind `email` can be renamed and even reassigned to another
    # person. Email stays unique too, so two rows can never share an address —
    # NULLs are distinct in Postgres, so users with no Entra link are unaffected.
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        UniqueConstraint("tenant_id", "ms_object_id", name="uq_users_tenant_ms_object_id"),
        # Children reference (tenant_id, id) so their FK cannot cross agencies —
        # the same idiom `clients` carries as `uq_clients_tenant_id_id`. Declared
        # here as well as in the migration so autogenerate does not propose
        # dropping it.
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id_id"),
        # Partial unique index: guarantees at most one owner per tenant.
        # This is what enables irreversible deletion of a candidate's personal data
        # (only the owner can authorize it). Declared here so autogenerate does not
        # propose dropping it; the index is created in the owner-role migration.
        Index(
            "uq_users_one_owner_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
        ),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    # The user's own choice of name, distinct from `display_name` — which
    # sign-in overwrites from the provider's claims on every login (see the
    # upsert in app/api/auth.py). Mirrors `CandidateFieldOverride`: the
    # provider's fact and the person's decision are kept in separate places so
    # neither can silently clobber the other. Written only by
    # `PATCH /api/auth/me`; sign-in must never touch this column.
    preferred_name: Mapped[str | None] = mapped_column(String(255))
    # Entra ID object id ("oid" claim) — stable per user per tenant.
    ms_object_id: Mapped[str | None] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="recruiter")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship(back_populates="users")
