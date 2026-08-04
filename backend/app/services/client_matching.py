"""Deciding which client an email is about.

The company named in the body is the authority, not the sender's domain.
A recruitment agency forwards dozens of job orders from different hiring
companies, and keying on the sender's domain collapses all of them into one
client — the agency itself. So the body's company name is tried first;
only when no company is named does the sender's domain get a turn (a direct
email from a company that didn't get its name extracted is still better
keyed on domain than on nothing).

When a client is created from a body company name, the sender's domain is
NOT attached: the sender is frequently an intermediary, and pinning their
domain onto the hiring company's identity key would be a fabrication. The
client's real domain can be filled in later by a recruiter.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.services.client_naming import domain_of, normalize_company_name

# Isolation is defence in depth here, not a single mechanism — the same
# arrangement `candidate_matching.py` documents. RLS on `clients` (keyed off
# the `app.tenant_id` setting `tenant_session` establishes) is what actually
# enforces the boundary; the explicit `tenant_id = :tenant_id` predicate below
# is a second, independent guard against a session that was never scoped. It
# fails closed: an unscoped session returns no rows rather than another
# agency's. Stated in both matchers deliberately, so a reader of either one is
# not left believing the two modules disagree about what guarantees isolation.
#
# Merged rows are deprioritised, not excluded: the partial unique index still
# lets several merged rows share a domain, but `_surviving` will redirect
# through whichever one we pick anyway. Archived rows must stay eligible —
# they hold the index slot, so skipping them would send the matcher to the
# insert path and into a unique violation.
_BY_DOMAIN = text(
    """
    SELECT id, status, merged_into_client_id, assigned_user_id FROM clients
    WHERE tenant_id = :tenant_id AND email_domain = :domain
    ORDER BY (status = 'merged') ASC, last_seen_at DESC NULLS LAST, created_at DESC
    LIMIT 1
    """
)

# Same shape as `_BY_DOMAIN`: merged rows are deprioritised, not excluded, so
# `_surviving` can follow the chain. A name-created client has no domain to
# fall back on (the sender's domain is not attached — see the module
# docstring), so excluding merged rows here would strand a merged client with
# no way back to its survivor.
_BY_NAME = text(
    """
    SELECT id, status, merged_into_client_id, assigned_user_id FROM clients
    WHERE tenant_id = :tenant_id AND name_normalized = :name
    ORDER BY (status = 'merged') ASC, last_seen_at DESC NULLS LAST, created_at DESC
    LIMIT 1
    """
)

# Domain-keyed insert: the partial unique index backs the ON CONFLICT, so two
# concurrent workers hitting the same domain resolve to one row.
_INSERT_CLIENT_BY_DOMAIN = text(
    """
    INSERT INTO clients
        (id, tenant_id, name, name_normalized, email_domain, status,
         first_seen_email_message_id, last_seen_at, assigned_user_id)
    VALUES (:id, :tenant_id, :name, :name_normalized, :domain, 'unconfirmed',
            :message_id, now(), :assigned_user_id)
    ON CONFLICT (tenant_id, email_domain)
        WHERE email_domain IS NOT NULL AND status <> 'merged'
    DO UPDATE SET last_seen_at = now()
    RETURNING id, assigned_user_id
    """
)

# Name-keyed insert: there is no unique index on name_normalized (by design —
# two unrelated firms can normalise to the same string), so the ON CONFLICT
# trick above cannot apply. Instead a WHERE NOT EXISTS guards the insert:
# whichever worker loses the race inserts nothing, and the caller re-reads
# the row the winner created. The race costs one extra SELECT, not a
# duplicate row.
_INSERT_CLIENT_BY_NAME = text(
    """
    INSERT INTO clients
        (id, tenant_id, name, name_normalized, email_domain, status,
         first_seen_email_message_id, last_seen_at, assigned_user_id)
    SELECT :id, :tenant_id, :name, :name_normalized, NULL, 'unconfirmed',
           :message_id, now(), :assigned_user_id
    WHERE NOT EXISTS (
        SELECT 1 FROM clients
        WHERE tenant_id = :tenant_id AND name_normalized = :name_normalized
    )
    RETURNING id, assigned_user_id
    """
)

_TOUCH = text("UPDATE clients SET last_seen_at = now() WHERE id = :id")

_STATUS_OF = text(
    "SELECT status, merged_into_client_id, assigned_user_id FROM clients WHERE id = :id"
)

# Bound on merge-chain hops. Each hop is a manual merge action a person took;
# a tenant would need this many merges in a row before the bound bites, and
# hitting it means a cycle (or a pathological chain) rather than genuine
# depth, so we stop and attach to the last row seen instead of looping
# forever.
_MAX_MERGE_HOPS = 50

# The unique constraint makes a repeated mention a no-op. `DO NOTHING` rather
# than an existence check, because two workers can reach this line at once.
_INSERT_MENTION = text(
    """
    INSERT INTO client_mentions (id, tenant_id, client_id, email_message_id, matched_by)
    VALUES (:id, :tenant_id, :client_id, :message_id, :matched_by)
    ON CONFLICT (tenant_id, client_id, email_message_id) DO NOTHING
    """
)

# A domain-matched sender is a person at that company — capture them as a
# contact. No unique index backs (tenant_id, client_id, email) on
# `client_contacts` (the only unique index there is the partial one on the
# single primary), so dedup is `WHERE NOT EXISTS` in the same statement rather
# than an ON CONFLICT. Additive only, matching `apply_contacts` in
# client_discovery: a contact the client already has is left exactly as a
# recruiter keeps it. `is_primary` is never set by the pipeline — promoting is
# a person's call, and the partial unique index makes a concurrent
# auto-promote unsafe.
_INSERT_CONTACT = text(
    """
    INSERT INTO client_contacts (id, tenant_id, client_id, name, email, is_primary)
    SELECT :id, :tenant_id, :client_id, :name, :email, false
    WHERE NOT EXISTS (
        SELECT 1 FROM client_contacts
        WHERE tenant_id = :tenant_id AND client_id = :client_id
          AND lower(coalesce(email, '')) = lower(:email)
    )
    """
)

# --- Buddy capture (forwarded-email original senders) ---

# Is the original sender actually the user (one of their declared aliases)?
# If so, this is the user forwarding from their own work address — not a
# buddy. Checked before any buddy row is written.
_USER_EMAIL_MATCH = text(
    "SELECT 1 FROM user_emails WHERE tenant_id = :tenant_id AND lower(email) = lower(:email)"
)

# Upsert a buddy keyed on email. Emails are stored lowercased so the
# (tenant_id, email) unique constraint catches repeats. ON CONFLICT DO
# NOTHING — the row the pipeline created first keeps its name and domain.
_UPSERT_BUDDY = text(
    """
    INSERT INTO buddies (id, tenant_id, name, email, email_domain, source)
    VALUES (:id, :tenant_id, :name, :email, :domain, 'pipeline')
    ON CONFLICT (tenant_id, email) DO NOTHING
    RETURNING id
    """
)

# Fallback when ON CONFLICT fired (RETURNING yields nothing): re-read.
_BUDDY_BY_EMAIL = text(
    "SELECT id FROM buddies WHERE tenant_id = :tenant_id AND lower(email) = lower(:email)"
)

# One referral per (buddy, client). ON CONFLICT DO NOTHING — a buddy who
# forwards three times for the same client is still one referral.
_INSERT_REFERRAL = text(
    """
    INSERT INTO buddy_referrals (id, tenant_id, buddy_id, client_id, email_message_id)
    VALUES (:id, :tenant_id, :buddy_id, :client_id, :message_id)
    ON CONFLICT (tenant_id, buddy_id, client_id) DO NOTHING
    """
)


@dataclass(frozen=True)
class MatchedClient:
    """The client this email is about, and who at the agency looks after it.

    The assignee travels with the id because ingestion needs both and the
    matcher has already got the row in hand — asking for it again would be a
    second query for something we just read. `assigned_user_id` is None when
    the client is real but nobody owns it yet: that is queue work, not an
    error.

    `matched_by` records how the match was decided — "email_domain" or "name"
    — so the caller can tell a domain fact from a name resemblance. Contact
    capture uses it: only a domain match is strong enough evidence that the
    sender is a person at that company.
    """

    client_id: uuid.UUID
    assigned_user_id: uuid.UUID | None
    matched_by: str


async def match_client(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID | None,
    sender_email: str | None,
    company_name: str | None,
    *,
    mailbox_owner_id: uuid.UUID | None = None,
    sender_name: str | None = None,
    original_sender_email: str | None = None,
    original_sender_name: str | None = None,
) -> MatchedClient | None:
    """Resolve this email to a client, recording how.

    Returns None when the email offers neither a usable domain nor a company
    name. That is a real outcome, not an error: a message can legitimately
    mention no company, and inventing a client for it would be exactly the
    fabrication the pipeline exists to avoid (§15).

    `mailbox_owner_id` is the recruiter whose mailbox received the email — the
    person the client emailed to. It is written onto a *new* client only: the
    `_INSERT_CLIENT` ON CONFLICT clause updates `last_seen_at` and nothing
    else, so a client that already exists keeps the owner it was first given.
    That is the "first person the client emailed to" rule — a forward to a
    colleague never reassigns.

    Contact capture uses the *original sender* when one is available (a
    forwarded email — the original sender has the client relationship, not the
    forwarder). For a direct email (no original sender), the envelope sender
    is the client's contact and their domain is attached to the client.
    """
    domain = domain_of(sender_email)
    normalized = normalize_company_name(company_name) if company_name else ""

    if domain is None and not normalized:
        return None

    client_id, assigned_user_id, matched_by = await _resolve(
        session, tenant_id, domain, normalized, company_name, email_message_id,
        mailbox_owner_id,
    )
    if client_id is None:
        return None

    await session.execute(
        _INSERT_MENTION,
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "client_id": client_id,
            "message_id": email_message_id,
            "matched_by": matched_by,
        },
    )

    # A forwarded email's original sender is a buddy (an external recruiter
    # who referred this client), not a client contact. A direct email's
    # envelope sender IS at the client's company when matched by domain —
    # that is a real client contact.
    if original_sender_email:
        await _capture_buddy(
            session, tenant_id, client_id, email_message_id,
            original_sender_email, original_sender_name,
        )
    elif matched_by == "email_domain" and sender_email:
        await _capture_contact(
            session, tenant_id, client_id, sender_email, sender_name
        )

    return MatchedClient(
        client_id=client_id, assigned_user_id=assigned_user_id, matched_by=matched_by
    )


async def _resolve(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    domain: str | None,
    normalized: str,
    company_name: str | None,
    email_message_id: uuid.UUID | None,
    mailbox_owner_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, str]:
    # The company named in the body is the authority. A forwarded job order
    # names the hiring company; the sender is frequently an intermediary
    # whose domain is the agency's, not the client's. Trying domain first
    # collapsed every forwarded order onto the forwarding agency.
    if normalized:
        row = (
            await session.execute(_BY_NAME, {"tenant_id": tenant_id, "name": normalized})
        ).first()
        if row is not None:
            client_id, assignee = await _surviving(session, row)
            return client_id, assignee, "name"

        # New client from the body company name. The sender's domain is NOT
        # attached — see the module docstring. No unique index backs
        # name_normalized (by design), so a WHERE NOT EXISTS guards the race
        # and the caller re-reads if it lost.
        params = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "name": company_name.strip(),
            "name_normalized": normalized,
            "message_id": email_message_id,
            "assigned_user_id": mailbox_owner_id,
        }
        inserted = (
            await session.execute(_INSERT_CLIENT_BY_NAME, params)
        ).first()
        if inserted is not None:
            return inserted.id, inserted.assigned_user_id, "name"
        # Lost the race — another worker inserted first. Re-read it.
        row = (
            await session.execute(_BY_NAME, {"tenant_id": tenant_id, "name": normalized})
        ).first()
        if row is not None:
            client_id, assignee = await _surviving(session, row)
            return client_id, assignee, "name"
        return None, None, ""

    # No company in the body — fall back to the sender's domain. A direct
    # email from a company that didn't get its name extracted is still
    # better keyed on domain than on nothing.
    if domain is not None:
        row = (
            await session.execute(
                _BY_DOMAIN, {"tenant_id": tenant_id, "domain": domain}
            )
        ).first()
        if row is not None:
            client_id, assignee = await _surviving(session, row)
            return client_id, assignee, "email_domain"

        inserted = (
            await session.execute(
                _INSERT_CLIENT_BY_DOMAIN,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "name": domain,
                    "name_normalized": domain,
                    "domain": domain,
                    "message_id": email_message_id,
                    "assigned_user_id": mailbox_owner_id,
                },
            )
        ).one()
        return inserted.id, inserted.assigned_user_id, "email_domain"

    return None, None, ""


async def _surviving(session: AsyncSession, row) -> tuple[uuid.UUID, uuid.UUID | None]:
    """The row a match should attach to, following the merge chain to its end.

    A match never changes status. Re-seeing an archived client records that it
    was seen and leaves it archived — un-archiving is a judgement about whether
    the agency still works with that company, which is a person's to make.
    """
    status, client_id, target = row.status, row.id, row.merged_into_client_id
    assignee = row.assigned_user_id
    hops = 0
    while status == Client.MERGED and target is not None and hops < _MAX_MERGE_HOPS:
        client_id = target
        next_row = (await session.execute(_STATUS_OF, {"id": client_id})).first()
        if next_row is None:
            break
        # The assignee travels with the surviving row, not the merged one: a
        # merged client's identity — including who looks after it — now
        # belongs to its target.
        status, target = next_row.status, next_row.merged_into_client_id
        assignee = next_row.assigned_user_id
        hops += 1
    await session.execute(_TOUCH, {"id": client_id})
    return client_id, assignee


async def _capture_contact(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    sender_email: str,
    sender_name: str | None,
) -> None:
    """Record the sender as a contact of the client, if not already known.

    Only reached on a domain match — the caller has already established the
    sender belongs to this company. The check is case-insensitive on email,
    matching how `apply_contacts` in client_discovery deduplicates: a contact
    the client already has is left untouched, never overwritten. The whole
    insert-and-dedup is one statement, so two concurrent extractions of the
    same sender cannot each miss the other and insert twice.
    """
    await session.execute(
        _INSERT_CONTACT,
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "client_id": client_id,
            "name": (sender_name or "").strip() or sender_email,
            "email": sender_email,
        },
    )


async def _capture_buddy(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    email_message_id: uuid.UUID | None,
    sender_email: str,
    sender_name: str | None,
) -> None:
    """Record the original sender of a forward as a buddy who referred the client.

    First checks whether the sender is actually the user (via their declared
    email aliases) — a user forwarding from their own work address is not a
    buddy. Otherwise upserts the buddy and creates a referral link.

    Runs inside the extraction transaction, so a buddy whose referral
    triggers a rollback never exists outside it.
    """
    is_self = (
        await session.execute(
            _USER_EMAIL_MATCH,
            {"tenant_id": tenant_id, "email": sender_email},
        )
    ).first()
    if is_self:
        return

    domain = sender_email.rsplit("@", 1)[-1].lower() if "@" in sender_email else None
    inserted = (
        await session.execute(
            _UPSERT_BUDDY,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "name": (sender_name or "").strip() or sender_email,
                "email": sender_email.lower(),
                "domain": domain,
            },
        )
    ).first()
    if inserted is not None:
        buddy_id = inserted.id
    else:
        row = (
            await session.execute(
                _BUDDY_BY_EMAIL,
                {"tenant_id": tenant_id, "email": sender_email.lower()},
            )
        ).first()
        if row is None:
            return
        buddy_id = row.id

    await session.execute(
        _INSERT_REFERRAL,
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "buddy_id": buddy_id,
            "client_id": client_id,
            "message_id": email_message_id,
        },
    )


# Same shape as `_BY_NAME`: merged rows are excluded because a merged row's
# identity now belongs to its target, and the surviving row is what a manual
# opportunity should link to.
_BY_NAME_MANUAL = text(
    """
    SELECT id FROM clients
    WHERE tenant_id = :tenant_id AND name_normalized = :name AND status <> 'merged'
    ORDER BY last_seen_at DESC NULLS LAST, created_at DESC
    LIMIT 1
    """
)

_INSERT_CLIENT_MANUAL = text(
    """
    INSERT INTO clients
        (id, tenant_id, name, name_normalized, email_domain, status, source)
    VALUES (:id, :tenant_id, :name, :name_normalized, NULL, :status, :source)
    RETURNING id
    """
)


async def resolve_or_create_client_by_name(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    company_name_raw: str,
) -> uuid.UUID | None:
    """Turn a typed company name into a client id, matching or creating.

    Used by the manual opportunity route when a recruiter typed a company
    name but did not pick a client from the list. Deliberately name-only —
    a hand-typed vacancy carries no sender domain to match on, so this is
    the same second step `_resolve` takes, without the first.

    Runs inside the caller's transaction and does not commit: the opportunity
    insert that follows must be able to roll the client back with it, or a
    failed request would leave an orphan client behind.

    No unique constraint backs `name_normalized` (by design — see
    `Client.name_normalized`'s docstring), so nothing here closes the
    two-concurrent-creates race: two requests typing the same new name at
    once can each miss the other's SELECT and insert two rows. That is the
    same race `_resolve` above already accepts for the pipeline's own
    name-matching path; a duplicate here costs a recruiter a later manual
    merge, same as a duplicate there does, so it is tolerated rather than
    guarded with a second query or a lock this table was not built to take.
    """
    normalized = normalize_company_name(company_name_raw)
    if not normalized:
        return None

    row = (
        await session.execute(
            _BY_NAME_MANUAL, {"tenant_id": tenant_id, "name": normalized}
        )
    ).first()
    if row is not None:
        return row.id

    inserted = (
        await session.execute(
            _INSERT_CLIENT_MANUAL,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "name": company_name_raw.strip(),
                "name_normalized": normalized,
                "status": Client.CONFIRMED,
                "source": Client.MANUAL,
            },
        )
    ).one()
    return inserted.id
