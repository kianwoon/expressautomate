"""Tenant and User — the multi-tenancy root (plan §18)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPrimaryKey


class Tenant(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    # Entra ID tenant this agency signed in from; null until first MS login.
    ms_tenant_id: Mapped[str | None] = mapped_column(String(64), index=True)

    users: Mapped[list["User"]] = relationship(back_populates="tenant")


class User(Base, UUIDPrimaryKey, Timestamps):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    # Entra ID object id ("oid" claim) — stable per user per tenant.
    ms_object_id: Mapped[str | None] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="recruiter")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship(back_populates="users")
