"""Stored Microsoft OAuth refresh tokens (plan §30).

One row per user who has signed in with Microsoft. The refresh token is the
standing grant that lets the ingestion worker read that user's mailbox long
after the sign-in request has ended, so it is stored encrypted (see
`app.core.crypto`) and never logged.
"""

import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class MicrosoftToken(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "ms_oauth_tokens"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_ms_tokens_tenant_user"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # MSAL's key for this identity in its own cache; kept so a future silent
    # token acquisition can find the account without another interactive login.
    home_account_id: Mapped[str | None] = mapped_column(String(128))
    # Refresh tokens have no fixed length and Entra's grow over time.
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # What the grant actually covers, which may be less than we asked for.
    scope: Mapped[str | None] = mapped_column(String(1024))
