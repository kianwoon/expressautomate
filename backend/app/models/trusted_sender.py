"""Sender domains the gate already trusts, per tenant.

Self-seeded by the classify jobs: a confident `recruitment` verdict upserts
the sender's domain here, and the next email from that domain skips the gate
LLM call. `domain` is exactly what `client_naming.domain_of` emits (lowercased,
personal-mail providers filtered out), so the two stay consistent.
"""

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class TrustedSender(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "trusted_senders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "domain", name="uq_trusted_senders_domain"),
    )

    domain: Mapped[str] = mapped_column(String(255), nullable=False)
