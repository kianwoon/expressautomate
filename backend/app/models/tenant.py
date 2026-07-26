"""Tenant and User — the multi-tenancy root (plan §18)."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint, text
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
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    # Entra ID object id ("oid" claim) — stable per user per tenant.
    ms_object_id: Mapped[str | None] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="recruiter")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship(back_populates="users")
