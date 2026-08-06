"""Seed the MOM Resident Occupational Wages reference library.

One-off idempotent backfill: read the published CSV, embed every occupation
title once, and upsert the rows into `mom_occupations`. The table is global
reference data (not tenant-scoped), so this writes under the admin role —
which has BYPASSRLS — rather than a `tenant_session`. The application role has
no INSERT/UPDATE grant and the table carries no DML policy, so a tenant session
cannot mutate it; this script is the one legitimate writer.

Idempotent by design: a row whose title already exists is skipped unless its
`embedding` is NULL, so re-running after a partial failure (or after a new CSV
vintage under a later `year`) embeds only what is missing and never re-bills
what is done. The unique constraint `uq_mom_occupations_year_title` makes a
duplicate insert an upsert conflict rather than an error.

The embedding provider lives on the worker, but this script runs in its own
process with the same env, so `settings.embedding_configured()` works as the
gate: a deployment without an embedding key loads the wage rows (with NULL
embeddings) so the chart can still show a benchmark once the recruiter manually
picks — and a later run with the key set fills the vectors in place.

Safety mirrors `tests/conftest.py` and `seed_clients.py`: refuses a non-local
host unless `--force` is given, writes nothing without `--write`.

    uv run python scripts/seed_mom_occupations.py --dry-run
    uv run python scripts/seed_mom_occupations.py --write
    uv run python scripts/seed_mom_occupations.py --write --force   # remote/prod
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.models.mom_occupation import MomOccupation  # noqa: E402
from app.services.llm.embeddings import embed_texts  # noqa: E402

# Same local-host allowance as tests/conftest.py and seed_clients.py. Kept as
# its own copy: tests/ is not on the runtime path, and a seed script quietly
# losing its guard because a test package moved is not a failure mode worth
# having.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db"}

_CSV = Path(__file__).resolve().parents[1] / "data" / "mom_resident_occupational_wages_2024.csv"


def remote_hosts(*urls: str) -> list[str]:
    """The hosts among `urls` that are not obviously disposable."""
    seen: list[str] = []
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if host and host not in _LOCAL_HOSTS and host not in seen:
            seen.append(host)
    return seen


def _load_rows() -> list[dict]:
    """Parse the CSV into typed row dicts, keyed by the model's column names.

    The CSV header uses the survey's own names (`occ_desc`,
    `mthly_gross_wage_50_pctile`); mapped here to the model's columns once so
    the insert site reads as plain kwargs. Empty/blank wage cells become a
    skip (a half-surveyed occupation is not benchmark material).
    """
    out: list[dict] = []
    with _CSV.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            try:
                row = {
                    "year": int(raw["year"]),
                    "title": (raw["occ_desc"] or "").strip().lower(),
                    "gross_p25": Decimal(raw["mthly_gross_wage_25_pctile"]),
                    "gross_p50": Decimal(raw["mthly_gross_wage_50_pctile"]),
                    "gross_p75": Decimal(raw["mthly_gross_wage_75_pctile"]),
                    "basic_p25": Decimal(raw["mthly_basic_wage_25_pctile"]),
                    "basic_p50": Decimal(raw["mthly_basic_wage_50_pctile"]),
                    "basic_p75": Decimal(raw["mthly_basic_wage_75_pctile"]),
                }
            except (ValueError, InvalidOperation, KeyError):
                continue
            if not row["title"]:
                continue
            out.append(row)
    return out


async def _existing_titles(session: AsyncSession, year: int) -> set[str]:
    """Titles already loaded for this vintage — the skip set for idempotency."""
    rows = (
        await session.execute(
            select(MomOccupation.title).where(MomOccupation.year == year)
        )
    ).scalars()
    return {r for r in rows}


async def _embed_missing(rows: list[dict]) -> dict[str, list[float]]:
    """Embed the titles of rows lacking a vector, batched.

    Returns `{title: vector}` for the titles that needed embedding. Skips
    entirely (returns {}) when embeddings are not configured — the caller then
    inserts wage rows with NULL embeddings so a manual pick still works, and a
    later run with the key set fills them.
    """
    if not settings.embedding_configured():
        print("Embeddings not configured — loading wage rows without vectors.")
        return {}

    titles = [r["title"] for r in rows]
    vectors: dict[str, list[float]] = {}
    batch = settings.EMBEDDING_BATCH_SIZE
    for i in range(0, len(titles), batch):
        chunk = titles[i : i + batch]
        result = await embed_texts(chunk)
        for title, vec in zip(chunk, result.vectors, strict=True):
            vectors[title] = vec
        print(f"  embedded {min(i + batch, len(titles))}/{len(titles)} titles")
    return vectors


async def _run(args: argparse.Namespace) -> int:
    offenders = remote_hosts(settings.alembic_url)
    if offenders and not args.force:
        print(
            "Refusing to run against a non-local host "
            f"({', '.join(offenders)}). Use --force to allow.",
            file=sys.stderr,
        )
        return 2

    rows = _load_rows()
    if not rows:
        print(f"No rows parsed from {_CSV}.", file=sys.stderr)
        return 1

    # All rows in this vintage share one year (the survey publishes per-year).
    year = rows[0]["year"]
    admin_engine = create_async_engine(
        settings.alembic_url,
        connect_args=settings.asyncpg_connect_args,
        pool_pre_ping=True,
    )
    AdminSession = async_sessionmaker(
        admin_engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with AdminSession() as session:
            existing = await _existing_titles(session, year)
        missing = [r for r in rows if r["title"] not in existing]
        # Rows present but without an embedding get re-embedded on a later run
        # — a backfill interrupted mid-way leaves NULLs this picks up.
        if existing:
            async with AdminSession() as session:
                unembedded = (
                    await session.execute(
                        select(MomOccupation.title).where(
                            MomOccupation.year == year,
                            MomOccupation.embedding.is_(None),
                        )
                    )
                ).scalars()
                need_vec = {t for t in unembedded}
        else:
            need_vec = set()
        to_embed = missing + [r for r in rows if r["title"] in need_vec]

        print(
            f"{year} survey: {len(rows)} parsed, {len(existing)} present, "
            f"{len(missing)} new, {len(need_vec)} re-embedding."
        )

        if args.dry_run:
            embed_note = (
                f"{len(to_embed)} embeddings would be generated."
                if settings.embedding_configured()
                else "embeddings not configured; rows would load without vectors."
            )
            print(
                f"[dry-run] would upsert {len(missing)} new rows; {embed_note} "
                "No writes performed."
            )
            return 0

        vectors = await _embed_missing(to_embed) if to_embed else {}

        inserted = 0
        embedded = 0
        async with AdminSession() as session:
            for r in rows:
                vec = vectors.get(r["title"])
                stmt = (
                    pg_insert(MomOccupation)
                    .values(
                        id=uuid.uuid4(),
                        year=r["year"],
                        title=r["title"],
                        gross_p25=r["gross_p25"],
                        gross_p50=r["gross_p50"],
                        gross_p75=r["gross_p75"],
                        basic_p25=r["basic_p25"],
                        basic_p50=r["basic_p50"],
                        basic_p75=r["basic_p75"],
                        embedding=vec,
                    )
                    .on_conflict_do_update(
                        constraint="uq_mom_occupations_year_title",
                        set_={
                            "gross_p25": r["gross_p25"],
                            "gross_p50": r["gross_p50"],
                            "gross_p75": r["gross_p75"],
                            "basic_p25": r["basic_p25"],
                            "basic_p50": r["basic_p50"],
                            "basic_p75": r["basic_p75"],
                            "embedding": vec,
                        },
                    )
                )
                result = await session.execute(stmt)
                if result.rowcount and vec is not None:
                    embedded += 1
                if r["title"] not in existing:
                    inserted += 1
            await session.commit()
        print(
            f"Wrote {inserted} new rows; {embedded} rows carry embeddings."
        )
    finally:
        await admin_engine.dispose()
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan, write nothing."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Upsert the rows. Required to change the database.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow running against a non-local (production) host.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.dry_run and not args.write:
        print("Neither --dry-run nor --write given; defaulting to --dry-run.")
        args.dry_run = True
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
