"""Sender-domain trust: skip the relevance gate for senders it already knows.

The gate is the highest-volume LLM call in the system, and most job orders
arrive from a handful of client domains the gate has already answered about.
`is_trusted_domain` lets a classify job answer `recruitment` for those emails
without paying the gate; `mark_trusted_domain` upserts the domain after a
confident verdict so the next email from it skips.

Both are deliberately fail-open. A missing trust row, a locked row, a write
failure — every error path here returns "not trusted", which sends the email
to the same gate that ran before this feature existed. Trust can only ever
*skip* a call; it can never drop a job order.

The threshold is the confidence of the verdict, not its status: an
`uncertain` verdict is the gate failing open, and seeding trust from it would
be trusting a domain the gate could not read. Only a verdict the gate
answered with a boolean (recruitment or not) seeds trust, and only a
`recruitment` verdict earns the "trusted" label — a domain that reliably
sends invoices is not a domain whose job orders we should skip the gate for.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.client_naming import domain_of

# allow-hardcode: SQL statements, not a phrase list.
_SELECT_TRUSTED = text(
    "SELECT 1 FROM trusted_senders WHERE tenant_id = :tenant_id AND domain = :domain"
)

_INSERT_TRUSTED = text(
    """
    INSERT INTO trusted_senders (id, tenant_id, domain)
    VALUES (:id, :tenant_id, :domain)
    ON CONFLICT (tenant_id, domain) DO NOTHING
    """
)


async def is_trusted_domain(
    session: AsyncSession, *, tenant_id: uuid.UUID, sender_email: str | None
) -> bool:
    """True when the gate has already answered `recruitment` for this domain.

    None or malformed sender (and any personal-mail provider, via
    `domain_of`) returns False, so the gate runs exactly as before.
    """
    domain = domain_of(sender_email)
    if not domain:
        return False
    row = (
        await session.execute(
            _SELECT_TRUSTED, {"tenant_id": tenant_id, "domain": domain}
        )
    ).first()
    return row is not None


async def mark_trusted_domain(
    session: AsyncSession, *, tenant_id: uuid.UUID, sender_email: str | None
) -> None:
    """Upsert a domain after a confident `recruitment` verdict.

    Idempotent: the unique constraint on (tenant_id, domain) plus
    ON CONFLICT DO NOTHING makes a repeat call a no-op. A malformed or
    personal-mail sender yields no domain and nothing is written.
    """
    domain = domain_of(sender_email)
    if not domain:
        return
    await session.execute(
        _INSERT_TRUSTED,
        {"id": uuid.uuid4(), "tenant_id": tenant_id, "domain": domain},
    )
