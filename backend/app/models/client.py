"""One company an agency recruits for.

A client is proposed by the pipeline and owned by a human. The distinction is
the whole design: `status` starts at `unconfirmed` and only a recruiter moves
it, because the evidence for "these two emails are the same company" is a
domain match at best and a normalised string at worst, and neither is a fact.

Identity is the sender's email domain. It is the only stable key the pipeline
actually has — a company renames itself in prose far more often than it
changes its mail domain. The normalised name exists to *propose* a match to a
person, never to make one.

Provenance lives in `client_mentions`, not here. This row is what a recruiter
edits; the mentions are the record of what the mail said, and one must not be
able to overwrite the other.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey


class Client(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    __tablename__ = "clients"

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    MERGED = "merged"
    ARCHIVED = "archived"
    SUSPENDED = "suspended"

    # How the row came to exist. Not inferable from
    # `first_seen_email_message_id`: that column is ON DELETE SET NULL, so a
    # retention purge would silently reclassify a pipeline client as manual.
    PIPELINE = "pipeline"
    MANUAL = "manual"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    # A hint for proposing a match to a person, never a key. Two unrelated
    # firms normalise to the same string often enough that a unique index here
    # would reject legitimate rows.
    name_normalized: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # NULL when the sender was on a free provider — see settings.FREE_EMAIL_DOMAINS.
    email_domain: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=UNCONFIRMED, index=True
    )
    merged_into_client_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    # SET NULL, not CASCADE: a client must outlive the email that produced it.
    first_seen_email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="SET NULL")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # Firm-level facts a recruiter maintains by hand. All nullable: an unset
    # field is "not recorded", and nothing infers a value for it (§15).
    website: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    # A percent, because that is what a recruiter quotes: 20.00.
    fee_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    payment_terms_days: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    # A suspension is a commercial hold on a client the agency still works
    # with. Both columns are cleared by `unsuspend` and by `archive`, so a
    # stale reason can never outlive the state it describes.
    suspended_reason: Mapped[str | None] = mapped_column(Text)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[str] = mapped_column(String(16), nullable=False, default=PIPELINE)

    # The recruiter who takes care of this account. Nullable in both
    # directions of the word: a pipeline-proposed client arrives with nobody
    # on it, and a departing recruiter's clients must outlive the account
    # rather than vanishing with it.
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    # The logo lives in R2; this names it. Nullable because most clients are
    # proposed by the pipeline and will never have one.
    logo_key: Mapped[str | None] = mapped_column(Text)
    # Lets the browser bust its own image cache without re-reading the object,
    # the same reason `candidates.avatar_updated_at` exists.
    logo_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Children reference (tenant_id, id) so their FK cannot cross agencies.
        UniqueConstraint("tenant_id", "id", name="uq_clients_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "merged_into_client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_clients_merged_into_same_tenant",
            ondelete="SET NULL",
        ),
        # Column-qualified SET NULL (PG15+): a plain SET NULL on a composite FK
        # nulls every referencing column, including `tenant_id`, which is
        # NOT NULL and would fail the delete. Only `assigned_user_id` clears —
        # same reasoning as `fk_opportunities_assignee_same_tenant`.
        ForeignKeyConstraint(
            ["tenant_id", "assigned_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_clients_assignee_same_tenant",
            ondelete="SET NULL (assigned_user_id)",
        ),
        # The identity key, declared here as well as in the migration so
        # autogenerate does not propose dropping it. `merged` is excluded so a
        # merge frees the domain for the surviving row; `archived` is
        # deliberately kept inside, because the matcher matches archived
        # clients and an excluded one would send it to its insert path and
        # into a violation of this very index.
        Index(
            "uq_clients_tenant_domain",
            "tenant_id",
            "email_domain",
            unique=True,
            postgresql_where=text("email_domain IS NOT NULL AND status <> 'merged'"),
        ),
    )


class ClientMention(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One email that referred to one client. The evidence trail.

    `ON DELETE SET NULL` on the message, so a retention purge of the mail body
    cannot erase the record that the client was ever seen. A mention with a
    null message id says "this happened and the source is gone", which is
    true; a deleted mention would say "this never happened", which is not.
    """

    __tablename__ = "client_mentions"

    client_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="SET NULL"), index=True
    )
    # No confidence column: `matched_by` already encodes match strength
    # honestly — a domain match is a fact about where the mail came from, a
    # name match is a resemblance. A per-mention numeric score would be a
    # fabricated probability (see the docstring in app/api/opportunities.py
    # on never rendering `model_confidence` as one).
    matched_by: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_client_mentions_client_same_tenant",
            ondelete="CASCADE",
        ),
        # One mention per client per message. `extract_email` re-runs after a
        # crash and replay appends; without this every rerun duplicates the
        # evidence and the mention count stops meaning anything.
        #
        # NULLS NOT DISTINCT, matching the migration: without it Postgres treats
        # every NULL `email_message_id` as unique and the rerun guard silently
        # stops applying to exactly the rows whose source email is gone.
        UniqueConstraint(
            "tenant_id",
            "client_id",
            "email_message_id",
            name="uq_client_mentions_once_per_message",
            postgresql_nulls_not_distinct=True,
        ),
    )


class ClientContact(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """One person at a client company.

    Deleted outright rather than status-flagged, unlike a `ClientMention`. A
    mention is evidence that something happened, and erasing it would assert
    that it never did; a contact is a current fact about who to call, and a
    stale one is worse than an absent one.
    """

    __tablename__ = "client_contacts"

    client_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        # Composite, so a contact cannot cross agencies — the same reason
        # `client_mentions` carries one.
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_client_contacts_client_same_tenant",
            ondelete="CASCADE",
        ),
        # At most one primary per client. A partial unique INDEX cannot be
        # DEFERRABLE (only constraints can be, and Postgres has no partial
        # unique constraint), so the demote statement must run before the
        # promote statement — see `_set_primary` in app/api/clients.py.
        Index(
            "uq_client_contacts_one_primary",
            "tenant_id",
            "client_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )


class ClientCollaborator(Base, UUIDPrimaryKey, TenantScoped, Timestamps):
    """A recruiter who covers this account besides the primary.

    Deliberately grants nothing. This is a record of who else knows the
    client, not a share: making it an implicit grant on the client's job
    orders would put a second, invisible path into the visibility predicate,
    and then "why can Raj see this?" would have two possible answers. Cover
    that needs sight of the work is an explicit share or a reassignment.

    There is no `is_primary` flag — the primary lives on `clients.assigned_user_id`,
    so there is one place to read it and no way for the two to disagree.
    """

    __tablename__ = "client_collaborators"

    client_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "client_id"],
            ["clients.tenant_id", "clients.id"],
            name="fk_client_collaborators_client_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_client_collaborators_user_same_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "client_id", "user_id", name="uq_client_collaborators_once"
        ),
    )
