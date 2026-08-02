"""Discovering client companies from mailbox headers (spec 2026-08-02).

Three concerns, kept in one module because they share one vocabulary:

1. **Scan** — walk the inbox and Sent Items over Graph with `$select` limited
   to sender/recipient and timestamp. Headers only: no bodies, no LLM, no
   `email_messages` rows, and no delta tokens — a second delta consumer would
   corrupt ingestion, so this walk pages a plain date-range filter instead.
2. **Rank** — the source plan's relationship score, weights from settings.
3. **Apply** — write contacts onto a client (existing or newly created).
   Non-destructive by construction: only contacts that are not already there
   are inserted, a primary is set only where none exists, and
   `last_seen_at` only ever moves forward.

Exclusion is layered, all of it configuration: `domain_of()` already refuses
malformed addresses and `FREE_EMAIL_DOMAINS` (the client-identity rule this
feature must agree with); on top of that sit the recruiter's own domain,
`CLIENT_DISCOVERY_EXCLUDED_DOMAINS` (suffix-matched) and
`CLIENT_DISCOVERY_SYSTEM_LOCALPARTS`.

Sessions are taken, never opened — the callers own the transaction, and the
explicit `tenant_id` predicates below are the same defence-in-depth
`client_matching.py` documents: RLS enforces the boundary, the predicate
fails closed if a session was never scoped.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.config import settings
from app.models.client import ClientContact
from app.services.client_naming import domain_of
from app.services.graph.client import MAILBOX_ROOT, GraphClient

# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class ContactSeen:
    """One person observed in the headers, keyed by lowercased address."""

    email: str
    name: str
    inbound: int = 0
    outbound: int = 0
    last_activity: datetime | None = None


@dataclass
class DomainSeen:
    """Everything the scan learned about one business domain."""

    domain: str
    received: int = 0
    sent: int = 0
    contacts: dict[str, ContactSeen] = field(default_factory=dict)
    last_activity: datetime | None = None


@dataclass(frozen=True)
class ScanResult:
    domains: dict[str, DomainSeen]
    inbox_scanned: int
    sent_scanned: int
    # The walk stopped at the message budget with pages still unread. Reported
    # rather than swallowed — a short list must never look like completeness.
    truncated: bool


def _suffix_match(domain: str, entries: frozenset[str]) -> bool:
    """Exact match, or a subdomain of an entry (`bounce.linkedin.com`)."""
    return any(domain == entry or domain.endswith("." + entry) for entry in entries)


def _system_localpart(address: str) -> bool:
    """Does the local part identify machinery rather than a person?

    The `+tag` is stripped first, so `noreply+billing@` is caught. An entry
    matches exactly, or as a prefix whose next character is not a letter —
    `noreply1` and `newsletter-team` match, a surname like `alertan` does not.
    """
    local = address.split("@", 1)[0].split("+", 1)[0].strip().lower()
    for entry in settings.CLIENT_DISCOVERY_SYSTEM_LOCALPARTS:
        if local == entry:
            return True
        if local.startswith(entry) and not local[len(entry)].isalpha():
            return True
    return False


def usable_domain(address: str | None, own_domains: frozenset[str]) -> str | None:
    """The domain a client could be keyed on, or None.

    `domain_of` already answers for malformed addresses and the free-provider
    list — reusing it keeps discovery in exact agreement with the identity
    rule the matcher applies to the same mail later.
    """
    if not address:
        return None
    domain = domain_of(address)
    if domain is None:
        return None
    if _suffix_match(domain, own_domains):
        return None
    if _suffix_match(domain, settings.CLIENT_DISCOVERY_EXCLUDED_DOMAINS):
        return None
    if _system_localpart(address):
        return None
    return domain


def _parse_dt(value: str | None) -> datetime | None:
    """Graph's ISO timestamp, or None — never a raise over one odd header."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _later(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def _note_contact(
    seen: DomainSeen,
    address: str,
    name: str | None,
    *,
    inbound: bool,
    when: datetime | None,
) -> None:
    email = address.strip().lower()
    contact = seen.contacts.get(email)
    if contact is None:
        # The display name when Graph has one; otherwise the address itself.
        # Never a value derived from the local part — that would be inventing
        # a name nobody wrote (§15).
        display = (name or "").strip()
        contact = ContactSeen(email=email, name=display or email)
        seen.contacts[email] = contact
    elif contact.name == contact.email and name and name.strip():
        # A later message finally carried a real display name; upgrade the
        # placeholder, and only the placeholder — a real name, once seen,
        # stays stable.
        contact.name = name.strip()
    if inbound:
        contact.inbound += 1
    else:
        contact.outbound += 1
    contact.last_activity = _later(contact.last_activity, when)
    seen.last_activity = _later(seen.last_activity, when)


async def _walk_folder(client: GraphClient, path: str, params: dict, per_item, remaining: int):
    """Page one folder, whole pages at a time, until done or out of budget.

    The budget is consulted between pages, like the delta walk: stopping
    mid-page and resuming from `@odata.nextLink` would skip every item after
    the stop position. Overshooting by at most one page is the cheaper error.
    """
    count = 0
    url: str | None = path
    first_params: dict | None = params
    while url:
        page = await client.get(url, params=first_params)
        # `@odata.nextLink` is absolute and carries every parameter itself.
        first_params = None
        for item in page.get("value", []):
            per_item(item)
            count += 1
        url = page.get("@odata.nextLink")
        if url and count >= remaining:
            return count, True
    return count, False


async def scan_headers(
    client: GraphClient, *, since: datetime, own_domains: frozenset[str]
) -> ScanResult:
    """Read the window's headers from both folders and aggregate per domain.

    A message sent to three people at one company is **one** interaction with
    that company (`sent` counts per message per domain) — but all three become
    contacts, which is the breadth signal `unique_contacts` scores.
    """
    stamp = since.isoformat().replace("+00:00", "Z")
    page_size = str(settings.CLIENT_DISCOVERY_PAGE_SIZE)
    budget = settings.CLIENT_DISCOVERY_MAX_MESSAGES
    domains: dict[str, DomainSeen] = {}

    def on_inbox(item: dict) -> None:
        sender = (item.get("from") or {}).get("emailAddress") or {}
        address = sender.get("address")
        domain = usable_domain(address, own_domains)
        if domain is None:
            return
        when = _parse_dt(item.get("receivedDateTime"))
        seen = domains.setdefault(domain, DomainSeen(domain=domain))
        seen.received += 1
        _note_contact(seen, address, sender.get("name"), inbound=True, when=when)

    def on_sent(item: dict) -> None:
        when = _parse_dt(item.get("sentDateTime"))
        counted: set[str] = set()
        for recipient in item.get("toRecipients") or []:
            entry = recipient.get("emailAddress") or {}
            address = entry.get("address")
            domain = usable_domain(address, own_domains)
            if domain is None:
                continue
            seen = domains.setdefault(domain, DomainSeen(domain=domain))
            if domain not in counted:
                seen.sent += 1
                counted.add(domain)
            _note_contact(seen, address, entry.get("name"), inbound=False, when=when)

    inbox_scanned, inbox_truncated = await _walk_folder(
        client,
        f"{MAILBOX_ROOT}/messages",
        {
            "$filter": f"receivedDateTime ge {stamp}",
            "$select": "from,receivedDateTime",
            "$top": page_size,
        },
        on_inbox,
        budget,
    )

    remaining = budget - inbox_scanned
    if remaining <= 0:
        # The inbox alone consumed the budget; Sent Items was never opened,
        # which is a truncation whether or not the inbox walk finished.
        return ScanResult(domains, inbox_scanned, 0, True)

    # allow-hardcode: `sentitems` is Graph's well-known folder name — an API
    # resource identifier like MAILBOX_ROOT, not a tunable.
    sent_scanned, sent_truncated = await _walk_folder(
        client,
        f"{MAILBOX_ROOT}/mailFolders/sentitems/messages",
        {
            "$filter": f"sentDateTime ge {stamp}",
            "$select": "toRecipients,sentDateTime",
            "$top": page_size,
        },
        on_sent,
        remaining,
    )

    return ScanResult(
        domains, inbox_scanned, sent_scanned, inbox_truncated or sent_truncated
    )


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def score(seen: DomainSeen, *, now: datetime) -> float:
    """The source plan's relationship score, weights from settings."""
    value = (
        seen.received * settings.CLIENT_DISCOVERY_WEIGHT_RECEIVED
        + seen.sent * settings.CLIENT_DISCOVERY_WEIGHT_SENT
        + len(seen.contacts) * settings.CLIENT_DISCOVERY_WEIGHT_UNIQUE_CONTACTS
    )
    recent_since = now - timedelta(days=settings.CLIENT_DISCOVERY_RECENCY_DAYS)
    if seen.last_activity is not None and seen.last_activity >= recent_since:
        value += settings.CLIENT_DISCOVERY_RECENCY_BONUS
    return value


def ranked_contacts(seen: DomainSeen) -> list[ContactSeen]:
    """Most active first; recency then address break ties, so the order is
    stable across runs over the same mail."""
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(
        seen.contacts.values(),
        key=lambda c: (
            -(c.inbound + c.outbound),
            -(c.last_activity or floor).timestamp(),
            c.email,
        ),
    )


def entry_for(seen: DomainSeen, *, now: datetime) -> dict:
    """One domain as the run's JSONB stores it.

    Contacts are capped here, at ranking time, because this is the only place
    the full per-domain list exists — a cap applied later would be discarding
    rows that were already paid for and stored.
    """
    cap = settings.CLIENT_DISCOVERY_MAX_CONTACTS_PER_CLIENT
    return {
        "domain": seen.domain,
        "score": round(score(seen, now=now), 2),
        "received": seen.received,
        "sent": seen.sent,
        "unique_contacts": len(seen.contacts),
        "last_activity": seen.last_activity.isoformat() if seen.last_activity else None,
        "created": False,
        "contacts": [
            {
                "email": c.email,
                "name": c.name,
                "inbound": c.inbound,
                "outbound": c.outbound,
                "last_activity": c.last_activity.isoformat() if c.last_activity else None,
            }
            for c in ranked_contacts(seen)[:cap]
        ],
    }


def ranked_entries(domains: dict[str, DomainSeen], *, now: datetime) -> list[dict]:
    entries = [entry_for(seen, now=now) for seen in domains.values()]
    entries.sort(key=lambda e: (-e["score"], e["domain"]))
    return entries


# ---------------------------------------------------------------------------
# Applying to the database
# ---------------------------------------------------------------------------

# Mirrors `client_matching._BY_DOMAIN` — merged rows deprioritised, archived
# eligible — but deliberately without the `last_seen_at` touch that
# `_surviving` performs: discovery records the timestamp it actually observed
# (GREATEST below), not the moment it happened to run.
# allow-hardcode: SQL statements, not a phrase list.
_BY_DOMAIN = text(
    """
    SELECT id, status, merged_into_client_id FROM clients
    WHERE tenant_id = :tenant_id AND email_domain = :domain
    ORDER BY (status = 'merged') ASC, last_seen_at DESC NULLS LAST, created_at DESC
    LIMIT 1
    """
)

_STATUS_OF = text("SELECT status, merged_into_client_id FROM clients WHERE id = :id")

# Same bound, same reasoning as `client_matching._MAX_MERGE_HOPS`: hitting it
# means a cycle rather than genuine depth, so stop and use the last row seen.
_MAX_MERGE_HOPS = 50

_TOUCH_SEEN = text(
    # GREATEST ignores a NULL side in Postgres, so this only ever moves the
    # timestamp forward — a re-scan can never claim a client was last seen
    # earlier than ingestion already knows it was.
    "UPDATE clients SET last_seen_at = GREATEST(last_seen_at, :seen)"
    " WHERE tenant_id = :tenant_id AND id = :id"
)

_EXISTING_CONTACTS = text(
    "SELECT id, email, is_primary FROM client_contacts"
    " WHERE tenant_id = :tenant_id AND client_id = :client_id"
)

_PROMOTE_CONTACT = text(
    "UPDATE client_contacts SET is_primary = true"
    " WHERE tenant_id = :tenant_id AND id = :id"
)

_APPLY_LOCK = text("SELECT pg_advisory_xact_lock(hashtext(:key))")

# The discovery upsert. Same conflict target as the pipeline's
# (`uq_clients_tenant_domain`), but the row a *person selected* arrives
# `confirmed`/`manual`, exactly as `POST /clients` creates one — and a
# pipeline row that got there first is promoted from `unconfirmed` only:
# archived and suspended are judgements this feature has no licence to undo.
# `name` is the domain itself; resolving a company name from a domain is the
# source plan's next phase, and anything else here would be a guess (§15).
_UPSERT_CLIENT = text(
    """
    INSERT INTO clients
        (id, tenant_id, name, name_normalized, email_domain, status, source,
         last_seen_at)
    VALUES (:id, :tenant_id, :name, :name_normalized, :domain, 'confirmed', 'manual',
            :seen)
    ON CONFLICT (tenant_id, email_domain)
        WHERE email_domain IS NOT NULL AND status <> 'merged'
    DO UPDATE SET
        last_seen_at = GREATEST(clients.last_seen_at, EXCLUDED.last_seen_at),
        status = CASE WHEN clients.status = 'unconfirmed'
                      THEN 'confirmed' ELSE clients.status END
    RETURNING id, (xmax = 0) AS created
    """
)


async def lock_contact_application(session, tenant_id: uuid.UUID) -> None:
    """Serialise contact writes per tenant for this transaction.

    `client_contacts` has no unique index on (client, email) — deliberately,
    two people can share an address role — so concurrent applications (a
    double-clicked Create racing itself, a scan racing a Create) would insert
    the same person twice. A transaction-scoped advisory lock is the cheapest
    statement that closes that, and it cannot outlive a crashed worker.
    """
    await session.execute(
        _APPLY_LOCK, {"key": f"client-discovery-apply:{tenant_id}"}
    )


async def existing_client_for_domain(
    session, tenant_id: uuid.UUID, domain: str
) -> uuid.UUID | None:
    """The live client this domain's mail attaches to, or None.

    Follows the merge chain exactly as ingestion's matcher does, so a domain
    whose row was merged enriches the surviving client rather than being
    offered back as "new" — the same mail would land on that client tomorrow.
    """
    row = (
        await session.execute(_BY_DOMAIN, {"tenant_id": tenant_id, "domain": domain})
    ).first()
    if row is None:
        return None
    client_id, status, target = row.id, row.status, row.merged_into_client_id
    hops = 0
    while status == "merged" and target is not None and hops < _MAX_MERGE_HOPS:
        client_id = target
        nxt = (await session.execute(_STATUS_OF, {"id": client_id})).first()
        if nxt is None:
            break
        status, target = nxt.status, nxt.merged_into_client_id
        hops += 1
    return client_id


async def apply_contacts(
    session, tenant_id: uuid.UUID, client_id: uuid.UUID, entry: dict
) -> int:
    """Write the entry's contacts onto a client. Returns rows inserted.

    Additive only: a contact whose address the client already has (case-
    insensitively) is left exactly as the recruiter keeps it — discovery never
    edits a row a person may have curated. The entry's top-ranked contact
    becomes primary only when the client has no primary at all; the partial
    unique index stays satisfied because nothing here demotes.
    """
    rows = (
        await session.execute(
            _EXISTING_CONTACTS, {"tenant_id": tenant_id, "client_id": client_id}
        )
    ).all()
    existing = {
        (row.email or "").strip().lower(): row.id for row in rows if row.email
    }
    has_primary = any(row.is_primary for row in rows)

    contacts = entry.get("contacts") or []
    top_email = contacts[0]["email"] if contacts else None

    added = 0
    for contact in contacts:
        email = contact["email"]
        if email in existing:
            continue
        session.add(
            ClientContact(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                client_id=client_id,
                name=contact["name"],
                email=email,
                is_primary=not has_primary and email == top_email,
            )
        )
        if not has_primary and email == top_email:
            has_primary = True
        added += 1

    if not has_primary and top_email and top_email in existing:
        # The most active sender is already on file, just never marked. There
        # is no primary to displace, so this is a promotion into a vacancy.
        await session.execute(
            _PROMOTE_CONTACT, {"tenant_id": tenant_id, "id": existing[top_email]}
        )

    if added:
        await session.flush()
    return added


async def enrich_existing_client(
    session, tenant_id: uuid.UUID, client_id: uuid.UUID, entry: dict
) -> int:
    """The automatic backfill for one already-known domain."""
    added = await apply_contacts(session, tenant_id, client_id, entry)
    if entry.get("last_activity"):
        await session.execute(
            _TOUCH_SEEN,
            {
                "tenant_id": tenant_id,
                "id": client_id,
                "seen": datetime.fromisoformat(entry["last_activity"]),
            },
        )
    return added


async def create_client_from_entry(
    session, tenant_id: uuid.UUID, entry: dict
) -> tuple[uuid.UUID, bool, int]:
    """Create (or adopt) the client for one selected domain, with contacts.

    Returns (client_id, created, contacts_added). `created` is False when the
    domain became a client between the scan and the click — the upsert then
    adopts that row (promoting only `unconfirmed`) instead of failing, and the
    contacts land on it all the same.
    """
    seen = (
        datetime.fromisoformat(entry["last_activity"])
        if entry.get("last_activity")
        else None
    )
    row = (
        await session.execute(
            _UPSERT_CLIENT,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                # The domain three times over, as three binds rather than one
                # reused parameter: asyncpg deduces ONE type per parameter,
                # and `name` (text) versus `email_domain` (varchar) made a
                # shared bind un-typeable.
                "name": entry["domain"],
                "name_normalized": entry["domain"],
                "domain": entry["domain"],
                "seen": seen,
            },
        )
    ).one()
    added = await apply_contacts(session, tenant_id, row.id, entry)
    return row.id, bool(row.created), added
