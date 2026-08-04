"""External recruiters who refer clients — the buddy network.

A buddy is someone at a partner agency who forwards job orders into the
user's mailbox. They are not the user's colleague (an ExpressAutomate User);
they are an external contact whose referrals the user works on. The
forwarding chain tells us who they are: the original sender of a forwarded
email is the buddy who referred that client.

Identity resolution uses the user's own email aliases: if the original
sender matches a declared alias, they ARE the user (forwarding to
themselves from their work address), not a buddy. The buddy's domain —
shared with one of the user's aliases — is what groups buddies into the
same agency.
"""

import uuid

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class UserEmail(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """An email address the user has claimed as their own.

    The primary ``users.email`` is set at sign-in from the identity provider
    and is the address the mailbox watches. Aliases are the user's other
    addresses — typically their work email at a partner agency — which the
    forwarding chain parser must recognise as "this is the user", not "this
    is a buddy forwarding to me".
    """

    __tablename__ = "user_emails"

    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Null until the user confirms the address is theirs (future: send a
    # verification email). The pipeline trusts declared aliases regardless,
    # because the user asserted ownership by adding the row — verification is
    # a defence against a typo'd address, not against a malicious claim.
    verified_at: Mapped[None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_emails_user_same_tenant",
            ondelete="CASCADE",
        ),
        # One alias per address per tenant — prevents two users in the same
        # agency claiming the same work email.
        UniqueConstraint("tenant_id", "email", name="uq_user_emails_tenant_email"),
    )


class Buddy(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """An external recruiter who forwards job orders into the user's mailbox.

    Created by the pipeline when a forwarding chain names a sender the
    system does not recognise as the user (via their aliases). The buddy's
    ``email_domain`` groups buddies from the same partner agency together.
    """

    __tablename__ = "buddies"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # The domain of the buddy's email — inferred at creation time so buddies
    # from the same agency can be grouped without parsing the email again.
    email_domain: Mapped[str | None] = mapped_column(String(255), index=True)
    PIPELINE = "pipeline"
    MANUAL = "manual"
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PIPELINE, server_default="pipeline"
    )

    __table_args__ = (
        # Children reference (tenant_id, id) so their FK cannot cross agencies.
        UniqueConstraint("tenant_id", "id", name="uq_buddies_tenant_id_id"),
        # One buddy per email per tenant. A buddy who forwards ten job orders
        # is one row, not ten.
        UniqueConstraint("tenant_id", "email", name="uq_buddies_tenant_email"),
    )


class BuddyReferral(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A client a buddy referred by forwarding a job order for it.

    One row per (buddy, client) pair — a buddy who forwards three job orders
    for the same client is one referral, not three. The ``email_message_id``
    is the first email that established the link (SET NULL on retention
    purge, so the referral outlives the source mail).
    """

    __tablename__ = "buddy_referrals"

    buddy_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("email_messages.id", ondelete="SET NULL"),
        index=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "buddy_id"],
            ["buddies.tenant_id", "buddies.id"],
            name="fk_buddy_referrals_buddy_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_buddy_referrals_client_same_tenant",
            ondelete="CASCADE",
        ),
        # One referral per buddy per client.
        UniqueConstraint(
            "tenant_id", "buddy_id", "client_id", name="uq_buddy_referrals_once"
        ),
    )
