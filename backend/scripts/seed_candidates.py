"""Seed development candidates for one tenant.

The sibling of `seed_clients.py`, and deliberately not a copy of it. A client
is *derived*: every seeded name comes from a `company_name_raw` an extraction
actually produced, because inventing one would put a company in the panel that
no email ever mentioned. A candidate has no such source. Email carries job
orders, not CVs (`models/candidate.py:1`), so there is nothing in the database
to derive a person from — every candidate here is written out in full, which is
honest rather than lazy: that is exactly how a real row is created, by a human
typing it.

The roster is aimed at the job orders already seeded for this tenant —
healthcare, govtech, tourism, logistics, tech — so the two screens describe one
agency instead of two disjoint fictions.

Addresses are minted under a caller-supplied placeholder suffix
(`--domain-suffix`, default a reserved-for-testing TLD from RFC 2606) so a
seeded address can never reach a real person. Some rows deliberately carry no
email, or no phone, because both columns are nullable and the list has to
render the gap.

Safety mirrors `seed_clients.py`, which mirrors `tests/conftest.py`: this
writes rows, so it refuses a host that is not obviously disposable unless the
caller says otherwise, and it writes nothing without an explicit opt-in flag.

    uv run python scripts/seed_candidates.py --tenant-id <uuid> --dry-run
    uv run python scripts/seed_candidates.py --tenant-id <uuid> --write
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.rls import tenant_session  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.models.candidate import Candidate  # noqa: E402
from app.services.candidate_naming import (  # noqa: E402
    normalize_email,
    normalize_phone,
    normalize_skill,
)

# Same allowance as tests/conftest.py, copied rather than imported for the
# reason given in seed_clients.py: `tests/` is not on the runtime path.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db"}

# The roster — sample content, not logic.
#
# allow-hardcode: these are fabricated sample *records* this script exists to
# insert, the seed equivalent of a fixture file. Nothing in the codebase reads,
# matches, or scores against this list; it is written once and inserted. There
# is no content-agnostic form of "twenty plausible people" — a generator would
# produce noise, and deriving them from the database is impossible because
# ingestion never carries CVs.
#
# Ordered so that slicing with --count keeps the spread of stages and the
# partial records rather than taking twenty rows of the same shape.
#
# Fields, in order: local part (None = no email), full name, phone as a
# recruiter typed it (None = no phone), title, employer, location, years,
# monthly salary, notice, employment type, pipeline stage, record status,
# skills.
#
# Phone numbers are Singapore mobiles (8/9 prefix — `is_matchable_phone` only
# treats those as identifying a person; a 6-prefix fixed line would be stored
# but would never dedupe). They are written in the mixed forms a sheet actually
# contains, so `phone_raw` shows the human string and `phone_e164` proves the
# normaliser ran.
_ROSTER: tuple[tuple, ...] = (
    (
        "priya.raman", "Priya Raman", "+65 9123 4471",
        "Senior Staff Nurse", "Parkway Shenton", "Singapore", 9, 5200,
        "1 month", "permanent", "contacted", Candidate.ACTIVE,
        ("Acute Care", "Triage", "Epic"),
    ),
    (
        "wei.jun.lim", "Lim Wei Jun", "9847 2210",
        "Backend Engineer", "GovTech Singapore", "Singapore", 6, 8800,
        "2 months", "permanent", "submitted", Candidate.ACTIVE,
        ("Python", "PostgreSQL", "Kubernetes"),
    ),
    (
        "nurul.aisyah", "Nurul Aisyah Binte Hassan", "+6588104932",
        "Guest Experience Manager", "Marina Bay Sands", "Singapore", 7, 6100,
        "1 month", "permanent", "new", Candidate.ACTIVE,
        ("Hospitality", "Team Leadership"),
    ),
    (
        # No phone. A CV that arrived as a PDF with only an address on it.
        "daniel.ong", "Daniel Ong Kai Sheng", None,
        "Logistics Operations Lead", "Nippon Express", "Jurong", 11, 7400,
        "3 months", "permanent", "placed", Candidate.ACTIVE,
        ("Freight Forwarding", "SAP EWM", "Customs Clearance"),
    ),
    (
        "shreya.nair", "Shreya Nair", "+65 9012 8845",
        "Solutions Consultant", "AvePoint", "Singapore", 5, 7900,
        "Immediate", "contract", "contacted", Candidate.ACTIVE,
        ("Microsoft 365", "SharePoint", "Pre-sales"),
    ),
    (
        # No email. A walk-in the recruiter took a number for.
        None, "Tan Hui Ling", "8221 7734",
        "Enrolled Nurse", "Raffles Medical", "Singapore", 3, 3400,
        "1 month", "permanent", "new", Candidate.ACTIVE,
        ("Patient Care", "Phlebotomy"),
    ),
    (
        "marcus.chia", "Marcus Chia Boon Hwee", "+65 9776 3018",
        "Head of Cyber Operations", "HTX", "Singapore", 16, 16500,
        "3 months", "permanent", "submitted", Candidate.ACTIVE,
        ("Threat Intelligence", "SOC", "Incident Response"),
    ),
    (
        "jasmine.koh", "Jasmine Koh Xin Yi", "9334 5561",
        "Tourism Marketing Executive", "Singapore Tourism Board", "Singapore", 2, 4200,
        "1 month", "contract", "rejected", Candidate.ACTIVE,
        ("Campaign Management", "Copywriting"),
    ),
    (
        "arjun.menon", "Arjun Menon", "+65 8459 2207",
        "Data Engineer", "Coda Payments", "Singapore", 4, 8200,
        "1 month", "permanent", "contacted", Candidate.ACTIVE,
        ("Airflow", "dbt", "BigQuery"),
    ),
    (
        "faridah.yusof", "Faridah Binte Yusof", "+65 9668 4130",
        "Clinic Manager", "Parkway Shenton", "Bukit Timah", 12, 6800,
        "2 months", "permanent", "placed", Candidate.ACTIVE,
        ("Clinic Operations", "Rostering", "Billing"),
    ),
    (
        "kelvin.sim", "Kelvin Sim Wee Kiat", "8907 1245",
        "Warehouse Supervisor", "YCH Group", "Tuas", 8, 4100,
        "1 month", "permanent", "new", Candidate.ACTIVE,
        ("WMS", "Forklift Licence", "Inventory Control"),
    ),
    (
        # Archived, and still holding both keys: an archived person keeps their
        # identity, which is why the unique indexes exclude only `merged`.
        "rachel.teo", "Rachel Teo Mei Ling", "+65 9223 6689",
        "Frontend Engineer", "Grab", "Singapore", 7, 9100,
        "2 months", "permanent", "contacted", Candidate.ARCHIVED,
        ("React", "TypeScript", "Accessibility"),
    ),
    (
        "hafiz.rahman", "Muhammad Hafiz Bin Rahman", "+65 8812 5507",
        "Paramedic", "SCDF", "Singapore", 6, 4600,
        "3 months", "permanent", "submitted", Candidate.ACTIVE,
        ("Emergency Response", "ACLS"),
    ),
    (
        "grace.wong", "Grace Wong Li Fen", None,
        "HR Business Partner", "Nippon Express", "Changi", 10, 7700,
        "2 months", "permanent", "new", Candidate.ACTIVE,
        ("Employee Relations", "Workday"),
    ),
    (
        "vignesh.kumar", "Vignesh Kumar", "9445 8890",
        "Site Reliability Engineer", "HTX", "Singapore", 9, 11200,
        "3 months", "permanent", "contacted", Candidate.ACTIVE,
        ("Terraform", "Observability", "Go"),
    ),
    (
        None, "Chua Yi Xuan", "+65 8156 7723",
        "Junior Recruiter", "Adecco", "Singapore", 1, 3200,
        "Immediate", "temporary", "new", Candidate.ACTIVE,
        ("Sourcing", "Screening"),
    ),
    (
        "elaine.foo", "Elaine Foo Sze Min", "+65 9538 2244",
        "Product Manager", "AvePoint", "Singapore", 8, 10400,
        "2 months", "permanent", "submitted", Candidate.ACTIVE,
        ("Roadmapping", "SaaS", "User Research"),
    ),
    (
        "samuel.lee", "Samuel Lee Jun Hao", "8663 9012",
        "Supply Chain Analyst", "Nippon Express", "Jurong", 3, 4900,
        "1 month", "contract", "rejected", Candidate.ACTIVE,
        ("Demand Planning", "Excel", "Power BI"),
    ),
    (
        "anitha.devi", "Anitha Devi d/o Suresh", "+65 9091 3376",
        "Senior Physiotherapist", "Raffles Medical", "Singapore", 13, 7200,
        "2 months", "permanent", "placed", Candidate.ACTIVE,
        ("Musculoskeletal", "Rehabilitation"),
    ),
    (
        "benjamin.tay", "Benjamin Tay Wei Sheng", "+65 8378 4419",
        "Tour Operations Manager", "Singapore Tourism Board", "Singapore", 14, 8600,
        "3 months", "permanent", "contacted", Candidate.ARCHIVED,
        ("Itinerary Design", "Vendor Management"),
    ),
)

_EXISTING = text(
    """
    SELECT lower(email) AS email, phone_e164
    FROM candidates
    WHERE tenant_id = :tenant_id AND record_status <> 'merged'
    """
)

# Candidates are created by a person, so `created_by` should name one. It is
# nullable and there may be no user yet, in which case NULL is the truthful
# value — better than pointing at somebody who did not do it.
_A_USER = text(
    "SELECT id::text FROM users WHERE tenant_id = :tenant_id ORDER BY created_at LIMIT 1"
)

_INSERT_CANDIDATE = text(
    """
    INSERT INTO candidates
        (id, tenant_id, full_name, email, phone_raw, phone_e164, current_title,
         current_employer, location, years_experience, expected_salary,
         salary_currency, salary_period, available_from, notice_period_raw,
         employment_type, notes, pipeline_stage, record_status, created_by, updated_by)
    VALUES (:id, :tenant_id, :full_name, :email, :phone_raw, :phone_e164, :current_title,
            :current_employer, :location, :years_experience, :expected_salary,
            :salary_currency, :salary_period, :available_from, :notice_period_raw,
            :employment_type, :notes, :pipeline_stage, :record_status, :created_by, :created_by)
    """
)

_INSERT_SKILL = text(
    """
    INSERT INTO candidate_skills (id, tenant_id, candidate_id, skill, skill_normalized)
    VALUES (:id, :tenant_id, :candidate_id, :skill, :skill_normalized)
    ON CONFLICT (tenant_id, candidate_id, skill_normalized) DO NOTHING
    """
)


@dataclass
class SeedCandidate:
    """One candidate row plus its skills, fully decided before anything writes.

    Same reason as `SeedClient`: building the plan first is what makes
    `--dry-run` honest — the run that prints and the run that inserts differ
    only in whether the INSERTs execute.
    """

    id: uuid.UUID
    full_name: str
    email: str | None
    phone_raw: str | None
    phone_e164: str | None
    current_title: str
    current_employer: str
    location: str
    years_experience: int
    expected_salary: int
    available_from: date
    notice_period_raw: str
    employment_type: str
    pipeline_stage: str
    record_status: str
    notes: str
    skills: tuple[str, ...] = field(default_factory=tuple)


def remote_hosts(*urls: str) -> list[str]:
    """The hosts among `urls` that are not obviously disposable.

    Pure and total, for the same reason conftest's twin is: the refusal path is
    the one a passing run never exercises.
    """
    seen: list[str] = []
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if host and host not in _LOCAL_HOSTS and host not in seen:
            seen.append(host)
    return seen


def plan(
    existing_emails: set[str],
    existing_phones: set[str],
    count: int,
    suffix: str,
    today: date,
) -> list[SeedCandidate]:
    """Decide every row to insert. No I/O, so the shape is testable by eye."""
    planned: list[SeedCandidate] = []
    seen_emails = set(existing_emails)
    seen_phones = set(existing_phones)

    for index, row in enumerate(_ROSTER):
        if len(planned) >= count:
            break
        (
            local, full_name, phone_raw, title, employer, location, years,
            salary, notice, employment_type, stage, status, skills,
        ) = row

        email = normalize_email(f"{local}@{suffix}") if local else None
        # Normalising here rather than trusting the literal is the point: if a
        # roster number were ever mistyped into something `phonenumbers`
        # rejects, this surfaces it instead of writing a half-parsed number
        # that would silently split or merge people.
        phone_e164 = normalize_phone(phone_raw)
        if phone_raw and phone_e164 is None:
            print(
                f"Skipping {full_name}: {phone_raw!r} is not a valid "
                f"{settings.DEFAULT_PHONE_REGION} number.",
                file=sys.stderr,
            )
            continue

        # Idempotency on the model's real dedupe keys — `lower(email)` and
        # `phone_e164` among non-merged rows, which are the two partial unique
        # indexes and the two lookups `find_candidate` does. A re-run must add
        # nobody who is already there; a collision would abort the whole
        # transaction rather than merely duplicate.
        if email and email in seen_emails:
            continue
        if phone_e164 and phone_e164 in seen_phones:
            continue
        if email:
            seen_emails.add(email)
        if phone_e164:
            seen_phones.add(phone_e164)

        planned.append(
            SeedCandidate(
                id=uuid.uuid4(),
                full_name=full_name,
                email=email,
                phone_raw=phone_raw,
                phone_e164=phone_e164,
                current_title=title,
                current_employer=employer,
                location=location,
                years_experience=years,
                expected_salary=salary,
                # Spread across the next quarter rather than all on one day, so
                # any sort or filter on availability has something to order.
                available_from=today + timedelta(days=14 + index * 5),
                notice_period_raw=notice,
                employment_type=employment_type,
                pipeline_stage=stage,
                record_status=status,
                notes=f"Seeded sample record. {title} at {employer}.",
                skills=skills,
            )
        )
    return planned


async def _read(session: AsyncSession, tenant_id: uuid.UUID):
    rows = (await session.execute(_EXISTING, {"tenant_id": tenant_id})).all()
    emails = {r.email for r in rows if r.email}
    phones = {r.phone_e164 for r in rows if r.phone_e164}
    user = (await session.execute(_A_USER, {"tenant_id": tenant_id})).first()
    return emails, phones, (uuid.UUID(user[0]) if user else None)


async def _write(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    planned: list[SeedCandidate],
    created_by: uuid.UUID | None,
    currency: str,
) -> None:
    """Insert candidates first, then their skills.

    Order matters: the skill FK points at (tenant_id, candidate_id). No row
    here is `merged`, so there is no self-reference to order around.
    """
    for c in planned:
        await session.execute(
            _INSERT_CANDIDATE,
            {
                "id": c.id,
                "tenant_id": tenant_id,
                "full_name": c.full_name,
                "email": c.email,
                "phone_raw": c.phone_raw,
                "phone_e164": c.phone_e164,
                "current_title": c.current_title,
                "current_employer": c.current_employer,
                "location": c.location,
                "years_experience": c.years_experience,
                "expected_salary": c.expected_salary,
                "salary_currency": currency,
                # Monthly, because that is how Singapore salaries are quoted —
                # and the column exists precisely so a monthly and an annual
                # figure never average into nonsense.
                "salary_period": "month",
                "available_from": c.available_from,
                "notice_period_raw": c.notice_period_raw,
                "employment_type": c.employment_type,
                "notes": c.notes,
                "pipeline_stage": c.pipeline_stage,
                "record_status": c.record_status,
                "created_by": created_by,
            },
        )
    for c in planned:
        for skill in c.skills:
            await session.execute(
                _INSERT_SKILL,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "candidate_id": c.id,
                    "skill": skill,
                    "skill_normalized": normalize_skill(skill),
                },
            )


def _report(planned: list[SeedCandidate], dry_run: bool) -> None:
    verb = "Would insert" if dry_run else "Inserted"
    skills = sum(len(c.skills) for c in planned)
    print(f"{verb} {len(planned)} candidates and {skills} skills:")
    for c in planned:
        print(
            f"  {c.record_status:<9} {c.pipeline_stage:<10} {c.full_name[:30]:<30} "
            f"{(c.email or '(no email)'):<38} {(c.phone_e164 or '(no phone)'):<14} "
            f"{c.current_title[:28]}"
        )


def _default_currency() -> str:
    """The tenant's first configured currency code. Never a literal here.

    `salary_currency` has to say something, and the project rule forbids a
    hardcoded one; the configured list's first entry is the deployment's own
    answer, and `--currency` overrides it.
    """
    codes = [c.strip() for c in settings.SALARY_CURRENCY_CODES.split(",") if c.strip()]
    return codes[0]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant-id", required=True, type=uuid.UUID)
    parser.add_argument("--count", type=int, default=len(_ROSTER))
    parser.add_argument(
        "--domain-suffix",
        default="example.invalid",
        help="Placeholder mail-domain suffix for seeded candidates. The default "
        "is reserved by RFC 2606 and can never resolve, so a seeded address can "
        "never reach a real person.",
    )
    parser.add_argument(
        "--currency",
        default=_default_currency(),
        help="Currency code for expected salaries. Defaults to the first entry "
        "of SALARY_CURRENCY_CODES.",
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
        # Refused even for a dry run, for the reason seed_clients.py gives: a
        # clean dry run against production reads as "the write is one flag
        # away", and it should not be.
        print(
            f"Refusing to touch database host(s): {', '.join(offenders)}.\n"
            "This script inserts rows. Point DATABASE_URL at a local or CI Postgres, "
            "or pass --i-know-this-is-remote if you truly mean it.",
            file=sys.stderr,
        )
        return 2

    # Disposed inside the same loop that opened the pool: asyncpg connections
    # belong to their loop.
    try:
        async with tenant_session(args.tenant_id) as session:
            emails, phones, created_by = await _read(session, args.tenant_id)
            planned = plan(emails, phones, args.count, args.domain_suffix, date.today())
            if planned and not dry_run:
                await _write(session, args.tenant_id, planned, created_by, args.currency)
    finally:
        await engine.dispose()

    if not planned:
        print("Every seeded candidate already exists for this tenant; nothing to do.")
        return 0
    _report(planned, dry_run)
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
