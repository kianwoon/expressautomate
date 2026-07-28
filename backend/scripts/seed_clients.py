"""Seed development clients derived from the job orders already in the database.

The point is that the seeded rows *line up* with real data. A client is never
invented here: every name comes from a `company_name_raw` an extraction
actually produced for this tenant, so the clients panel and the job-orders
table talk about the same companies rather than two disjoint fictions.

What is synthesised is only what a seed can legitimately synthesise: the
status spread a recruiter's queue would show after some review has happened,
one merge pair, and the mention rows that are the evidence trail. Mail domains
are minted under a caller-supplied placeholder suffix (`--domain-suffix`,
default a reserved-for-testing TLD) because the real sender domain lives on
`email_messages`, and guessing one would be exactly the fabrication §15
forbids.

Safety mirrors `tests/conftest.py`: this writes rows, so it refuses a host
that is not obviously disposable unless the caller says otherwise, and it
writes nothing at all without an explicit opt-in flag.

    uv run python scripts/seed_clients.py --tenant-id <uuid> --dry-run
    uv run python scripts/seed_clients.py --tenant-id <uuid> --write
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.rls import tenant_session  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.models.client import Client  # noqa: E402
from app.services.client_naming import normalize_company_name  # noqa: E402

# Same allowance as tests/conftest.py. Kept as its own copy rather than
# imported: `tests/` is not on the runtime path, and a seed script quietly
# losing its guard because a test package moved is not a failure mode worth
# having.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db"}

# How the statuses are dealt out, cycling. Unconfirmed dominates because that
# is what the review queue actually looks like — the default dashboard view is
# the proposals nobody has judged yet, and a seed that produced a tidy quarter
# of each would misrepresent the screen it exists to populate.
_STATUS_CYCLE = (
    Client.UNCONFIRMED,
    Client.UNCONFIRMED,
    Client.CONFIRMED,
    Client.UNCONFIRMED,
    Client.ARCHIVED,
    Client.CONFIRMED,
)

# Both values `client_matching.py` can write, alternated so the detail panel
# renders each. There is no third value: no code path records a manual match.
_MATCHED_BY = ("email_domain", "name")

_COMPANIES = text(
    """
    SELECT company_name_raw, MIN(email_message_id::text) AS email_message_id,
           MAX(received_datetime) AS last_seen_at, COUNT(*) AS orders
    FROM opportunities
    WHERE tenant_id = :tenant_id
      AND company_name_raw IS NOT NULL
      AND btrim(company_name_raw) <> ''
    GROUP BY company_name_raw
    ORDER BY orders DESC, company_name_raw
    """
)

# Extra message ids to hang a second mention off, so at least some clients show
# more than one line of evidence.
_MESSAGES = text(
    "SELECT id::text FROM email_messages WHERE tenant_id = :tenant_id "
    "ORDER BY received_datetime DESC NULLS LAST LIMIT :limit"
)

_EXISTING = text(
    "SELECT name_normalized, email_domain FROM clients WHERE tenant_id = :tenant_id"
)

_INSERT_CLIENT = text(
    """
    INSERT INTO clients
        (id, tenant_id, name, name_normalized, email_domain, status,
         merged_into_client_id, first_seen_email_message_id, last_seen_at)
    VALUES (:id, :tenant_id, :name, :name_normalized, :email_domain, :status,
            :merged_into_client_id, :first_seen_email_message_id,
            COALESCE(:last_seen_at, now()))
    """
)

_INSERT_MENTION = text(
    """
    INSERT INTO client_mentions (id, tenant_id, client_id, email_message_id, matched_by)
    VALUES (:id, :tenant_id, :client_id, :email_message_id, :matched_by)
    ON CONFLICT (tenant_id, client_id, email_message_id) DO NOTHING
    """
)


@dataclass
class SeedClient:
    """One client row plus its mentions, fully decided before anything writes.

    Building the whole plan first is what makes `--dry-run` honest: the run
    that prints and the run that inserts differ only in whether the INSERTs
    are executed.
    """

    id: uuid.UUID
    name: str
    name_normalized: str
    email_domain: str | None
    status: str
    first_seen_email_message_id: uuid.UUID | None
    last_seen_at: object | None
    merged_into_client_id: uuid.UUID | None = None
    mentions: list[tuple[uuid.UUID | None, str]] = field(default_factory=list)


def remote_hosts(*urls: str) -> list[str]:
    """The hosts among `urls` that are not obviously disposable.

    Pure and total, for the same reason conftest's twin is: the refusal path
    is the one a passing run never exercises.
    """
    seen: list[str] = []
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if host and host not in _LOCAL_HOSTS and host not in seen:
            seen.append(host)
    return seen


def slug_for(name_normalized: str, suffix: str) -> str | None:
    """A placeholder mail domain, or None when nothing usable survives.

    None is a real outcome and not a defect: `email_domain` is nullable
    precisely because the pipeline often has no domain, and a client with no
    domain exercises the name-match path the UI also has to render.
    """
    kept = [c for c in name_normalized.lower() if c.isalnum() or c == " "]
    slug = "-".join("".join(kept).split())[:48].strip("-")
    return f"{slug}.{suffix}" if slug else None


def unique_domain(base: str | None, taken: set[str]) -> str | None:
    """`base`, or the first free `-2`, `-3`, … variant of it. Records the result.

    Slugs collide for real reasons — the 48-character truncation, and two
    company names that differ only in punctuation normalising to the same
    string — and `uq_clients_tenant_domain` is a unique index, so a collision
    is not a duplicate row but an aborted transaction that rolls back the
    entire seed. `taken` is primed with the domains already in the table, so a
    second run cannot collide with the first one's output either.
    """
    if base is None:
        return None
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        stem, _, tld = base.partition(".")
        candidate = f"{stem}-{n}.{tld}"
    taken.add(candidate)
    return candidate


def plan(
    rows: list[tuple[str, str | None, object, int]],
    messages: list[str],
    existing: set[str],
    existing_domains: set[str],
    count: int,
    suffix: str,
) -> list[SeedClient]:
    """Decide every row to insert. No I/O, so the shape is testable by eye."""
    planned: list[SeedClient] = []
    seen_normalized = set(existing)
    taken_domains = set(existing_domains)

    for company_name, message_id, last_seen_at, _orders in rows:
        if len(planned) >= count:
            break
        normalized = normalize_company_name(company_name)
        if not normalized or normalized in seen_normalized:
            # Idempotency, and the reason for it: re-running must not add a
            # second row for a company a previous run (or the live pipeline)
            # already proposed. Normalised name is the comparison the matcher
            # itself uses to propose a merge.
            continue
        seen_normalized.add(normalized)

        index = len(planned)
        status = _STATUS_CYCLE[index % len(_STATUS_CYCLE)]
        # Every fourth client is left domainless, so the seeded set contains
        # the null-domain case the panel must handle.
        domain = (
            None
            if index % 4 == 3
            else unique_domain(slug_for(normalized, suffix), taken_domains)
        )

        client = SeedClient(
            id=uuid.uuid4(),
            name=company_name.strip(),
            name_normalized=normalized,
            email_domain=domain,
            status=status,
            first_seen_email_message_id=uuid.UUID(message_id) if message_id else None,
            last_seen_at=last_seen_at,
        )
        client.mentions = _mentions_for(client, messages, index)
        planned.append(client)

    _add_merge_pair(planned, messages, suffix, seen_normalized, taken_domains)
    return planned


def _mentions_for(
    client: SeedClient, messages: list[str], index: int
) -> list[tuple[uuid.UUID | None, str]]:
    """One mention per client, two for every third, both `matched_by` values.

    A mention with a null message id is included deliberately: it is what a
    retention purge leaves behind, and the panel renders it.
    """
    mentions: list[tuple[uuid.UUID | None, str]] = [
        (client.first_seen_email_message_id, _MATCHED_BY[index % len(_MATCHED_BY)])
    ]
    if index % 3 == 2 and messages:
        other = uuid.UUID(messages[index % len(messages)])
        if other != client.first_seen_email_message_id:
            mentions.append((other, _MATCHED_BY[(index + 1) % len(_MATCHED_BY)]))
    if index % 5 == 4:
        mentions.append((None, _MATCHED_BY[index % len(_MATCHED_BY)]))
    return mentions


def _add_merge_pair(
    planned: list[SeedClient],
    messages: list[str],
    suffix: str,
    seen_normalized: set[str],
    taken_domains: set[str],
) -> None:
    """Append a duplicate of a confirmed client, merged into it.

    The loser is a fresh row rather than a repurposed one so the pair is a
    genuine "the pipeline proposed this company twice, a recruiter merged
    them" — the case the merged filter exists for. The target is always
    `confirmed`, never itself merged, so no chain and no cycle is created:
    `merge_client` refuses a merged target, and `_surviving` would otherwise
    walk hops this seed had no business inventing.

    The loser keeps a domain of its own: the partial unique index excludes
    merged rows, so it cannot collide, and a merged row with a distinct domain
    is exactly what a duplicate proposal looked like before the merge.
    """
    target = next((c for c in planned if c.status == Client.CONFIRMED), None)
    if target is None:
        return
    normalized = f"{target.name_normalized} group"
    if normalized in seen_normalized:
        # A previous run already seeded this pair. Adding another loser would
        # give the merged filter a fresh duplicate on every partial re-run,
        # which is exactly the drift the idempotency check above prevents for
        # every other row.
        return
    seen_normalized.add(normalized)
    loser = SeedClient(
        id=uuid.uuid4(),
        name=f"{target.name} Group",
        name_normalized=normalized,
        email_domain=unique_domain(slug_for(normalized, suffix), taken_domains),
        status=Client.MERGED,
        merged_into_client_id=target.id,
        first_seen_email_message_id=target.first_seen_email_message_id,
        last_seen_at=target.last_seen_at,
    )
    loser.mentions = _mentions_for(loser, messages, 1)
    planned.append(loser)


async def _read(session: AsyncSession, tenant_id: uuid.UUID, count: int):
    rows = [
        (r.company_name_raw, r.email_message_id, r.last_seen_at, r.orders)
        for r in (await session.execute(_COMPANIES, {"tenant_id": tenant_id})).all()
    ]
    messages = [
        r[0]
        for r in (
            await session.execute(_MESSAGES, {"tenant_id": tenant_id, "limit": count * 2})
        ).all()
    ]
    seeded = (await session.execute(_EXISTING, {"tenant_id": tenant_id})).all()
    existing = {r[0] for r in seeded}
    existing_domains = {r[1] for r in seeded if r[1]}
    return rows, messages, existing, existing_domains


async def _write(session: AsyncSession, tenant_id: uuid.UUID, planned: list[SeedClient]) -> None:
    """Insert clients first, then mentions.

    Order matters twice over: the mention FK points at (tenant_id, client_id),
    and a merged row's target must already exist for
    `fk_clients_merged_into_same_tenant`. The merge pair is planned last, so
    plan order is already insert order.
    """
    for client in planned:
        await session.execute(
            _INSERT_CLIENT,
            {
                "id": client.id,
                "tenant_id": tenant_id,
                "name": client.name,
                "name_normalized": client.name_normalized,
                "email_domain": client.email_domain,
                "status": client.status,
                "merged_into_client_id": client.merged_into_client_id,
                "first_seen_email_message_id": client.first_seen_email_message_id,
                "last_seen_at": client.last_seen_at,
            },
        )
    for client in planned:
        for message_id, matched_by in client.mentions:
            await session.execute(
                _INSERT_MENTION,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "client_id": client.id,
                    "email_message_id": message_id,
                    "matched_by": matched_by,
                },
            )


def _report(planned: list[SeedClient], dry_run: bool) -> None:
    verb = "Would insert" if dry_run else "Inserted"
    mentions = sum(len(c.mentions) for c in planned)
    print(f"{verb} {len(planned)} clients and {mentions} mentions:")
    for client in planned:
        target = f" -> {client.merged_into_client_id}" if client.merged_into_client_id else ""
        print(
            f"  {client.status:<12} {client.name[:44]:<44} "
            f"{client.email_domain or '(no domain)':<40} "
            f"mentions={len(client.mentions)}{target}"
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument(
        "--domain-suffix",
        default="example.invalid",
        help="Placeholder mail-domain suffix for seeded clients. The default is "
        "reserved by RFC 2606 and can never resolve, so a seeded domain is "
        "never mistaken for a real one.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the plan, write nothing.")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually insert. Required; the default writes nothing.",
    )
    parser.add_argument(
        "--i-know-this-is-remote",
        action="store_true",
        help="Permit a database host that is not obviously disposable.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    dry_run = args.dry_run or not args.write
    offenders = remote_hosts(str(settings.DATABASE_URL))
    if offenders and not args.i_know_this_is_remote:
        # Refused even for a dry run: reading is harmless, but a caller who
        # gets a clean dry run against production will reasonably assume the
        # write is one flag away, and it should not be.
        print(
            f"Refusing to touch database host(s): {', '.join(offenders)}.\n"
            "This script inserts rows. Point DATABASE_URL at a local or CI Postgres, "
            "or pass --i-know-this-is-remote if you truly mean it.",
            file=sys.stderr,
        )
        return 2

    # Disposed inside the same loop that opened the pool: asyncpg connections
    # belong to their loop, and closing them from a second one is a warning at
    # best and a hang at worst.
    try:
        async with tenant_session(args.tenant_id) as session:
            rows, messages, existing, domains = await _read(session, args.tenant_id, args.count)
            if not rows:
                print(
                    f"No job orders with a company name for tenant {args.tenant_id}; "
                    "nothing to derive clients from.",
                    file=sys.stderr,
                )
                return 1
            planned = plan(
                rows, messages, existing, domains, args.count, args.domain_suffix
            )
            if planned and not dry_run:
                await _write(session, args.tenant_id, planned)
    finally:
        await engine.dispose()

    if not planned:
        print("Every company already has a client for this tenant; nothing to do.")
        return 0
    _report(planned, dry_run)
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
