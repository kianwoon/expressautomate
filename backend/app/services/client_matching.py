"""Deciding which client an email is about.

Three steps, first hit wins: the sender's domain, then the normalised company
name, then a new proposal. Only the first is an identity claim the pipeline is
entitled to make on its own — a domain is a fact about where the mail came
from. A name match records that the two look alike and leaves the row
unconfirmed for a person to judge.

The service takes a session rather than opening one. It runs inside the
extraction transaction in `persist()`, and a second connection would let the
extraction roll back while the client it proposed survived.
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

# Name matching ignores merged rows — a merged row's identity now belongs to
# its target — and prefers the most recently seen of any remaining ties.
_BY_NAME = text(
    """
    SELECT id, status, merged_into_client_id, assigned_user_id FROM clients
    WHERE tenant_id = :tenant_id AND name_normalized = :name AND status <> 'merged'
    ORDER BY last_seen_at DESC NULLS LAST, created_at DESC
    LIMIT 1
    """
)

_INSERT_CLIENT = text(
    """
    INSERT INTO clients
        (id, tenant_id, name, name_normalized, email_domain, status,
         first_seen_email_message_id, last_seen_at)
    VALUES (:id, :tenant_id, :name, :name_normalized, :domain, 'unconfirmed',
            :message_id, now())
    ON CONFLICT (tenant_id, email_domain)
        WHERE email_domain IS NOT NULL AND status <> 'merged'
    DO UPDATE SET last_seen_at = now()
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


@dataclass(frozen=True)
class MatchedClient:
    """The client this email is about, and who at the agency looks after it.

    The assignee travels with the id because ingestion needs both and the
    matcher has already got the row in hand — asking for it again would be a
    second query for something we just read. `assigned_user_id` is None when
    the client is real but nobody owns it yet: that is queue work, not an
    error.
    """

    client_id: uuid.UUID
    assigned_user_id: uuid.UUID | None


async def match_client(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    email_message_id: uuid.UUID | None,
    sender_email: str | None,
    company_name: str | None,
) -> MatchedClient | None:
    """Resolve this email to a client, recording how.

    Returns None when the email offers neither a usable domain nor a company
    name. That is a real outcome, not an error: a message can legitimately
    mention no company, and inventing a client for it would be exactly the
    fabrication the pipeline exists to avoid (§15).
    """
    domain = domain_of(sender_email)
    normalized = normalize_company_name(company_name) if company_name else ""

    if domain is None and not normalized:
        return None

    client_id, assigned_user_id, matched_by = await _resolve(
        session, tenant_id, domain, normalized, company_name, email_message_id
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
    return MatchedClient(client_id=client_id, assigned_user_id=assigned_user_id)


async def _resolve(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    domain: str | None,
    normalized: str,
    company_name: str | None,
    email_message_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, str]:
    if domain is not None:
        row = (
            await session.execute(
                _BY_DOMAIN, {"tenant_id": tenant_id, "domain": domain}
            )
        ).first()
        if row is not None:
            client_id, assignee = await _surviving(session, row)
            return client_id, assignee, "email_domain"

    if normalized:
        row = (
            await session.execute(_BY_NAME, {"tenant_id": tenant_id, "name": normalized})
        ).first()
        if row is not None:
            client_id, assignee = await _surviving(session, row)
            return client_id, assignee, "name"

    if not normalized:
        # A domain with no name still deserves a row; the domain is the name
        # we have, and labelling it anything else would be a guess.
        normalized = domain or ""

    inserted = (
        await session.execute(
            _INSERT_CLIENT,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "name": (company_name or domain or "").strip(),
                "name_normalized": normalized,
                "domain": domain,
                "message_id": email_message_id,
            },
        )
    ).one()
    return (
        inserted.id,
        inserted.assigned_user_id,
        "email_domain" if domain else "name",
    )


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
