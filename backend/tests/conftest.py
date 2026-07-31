"""Shared test fixtures.

Tests run against the real `expressautomate` database and clean up after
themselves; every fixture here exists to keep that honest.

Two connection paths are deliberately available:

- `app.db.session.SessionLocal` — the restricted runtime role, subject to RLS.
  Anything asserting isolation must use this, or it proves nothing.
- `admin_session` — the schema owner, which bypasses RLS. Used only to verify
  *schema-level* guarantees (foreign keys, cascades, unique constraints) that
  the policy would otherwise hide behind an empty result set.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.auth import SESSION_COOKIE, _session_serializer
from app.core.config import settings
from app.db.rls import tenant_session
from app.db.session import engine
from app.main import app
from app.models import Opportunity, User
from app.models.candidate import Candidate
from app.models.extraction import Extraction, ExtractionEvidence


def pytest_addoption(parser) -> None:
    """`--regenerate` rewrites the checked-in route manifest.

    Opt-in rather than automatic: a test that silently repaired the artefact it
    is asserting on could never fail, and that manifest is the only thing
    telling the frontend which URLs exist (see `test_route_manifest.py`).
    """
    parser.addoption(
        "--regenerate",
        action="store_true",
        default=False,
        help="rewrite checked-in fixtures (the route manifest) instead of asserting on them",
    )


# Hosts a test run is allowed to write to. Anything else is assumed to be real.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db"}


def remote_hosts(*urls: str) -> list[str]:
    """Return the hosts among `urls` that are not obviously disposable.

    Pure and total so both branches are unit-testable — CI only ever exercises
    the passing path, so the refusal itself would otherwise never be tested.
    """
    seen: list[str] = []
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if host and host not in _LOCAL_HOSTS and host not in seen:
            seen.append(host)
    return seen


def _refuse_to_run_against_a_remote_database() -> None:
    """Abort collection if any configured database is not obviously disposable.

    These tests INSERT and DELETE in `tenants` and `users`, and the schema-level
    ones use the admin role, which bypasses RLS. Pointed at a live database that
    is exactly a data-loss bug — and it already happened once, stranding test
    fixtures in production before this guard existed.

    Both URLs are checked. The admin URL is the dangerous one: it drives
    `AdminSessionLocal` below and bypasses RLS, so guarding only DATABASE_URL
    would still let a local-app-URL / production-admin-URL combination delete
    real rows.
    """
    offenders = remote_hosts(str(settings.DATABASE_URL), settings.alembic_url)
    if not offenders:
        return
    raise RuntimeError(
        f"Refusing to run the test suite against database host(s): {', '.join(offenders)}.\n"
        "The suite writes and deletes rows and uses the RLS-bypassing admin role.\n"
        "Point BOTH DATABASE_URL and DATABASE_ADMIN_URL at a local or CI Postgres "
        "(see docs/setup.md), e.g.:\n"
        "  docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres "
        "-e POSTGRES_DB=expressautomate --name ea-test-db postgres:16"
    )


_refuse_to_run_against_a_remote_database()

_admin_engine = create_async_engine(
    settings.alembic_url,
    connect_args=settings.asyncpg_connect_args,
    pool_pre_ping=True,
)
AdminSessionLocal = async_sessionmaker(_admin_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
async def _dispose_engines() -> AsyncGenerator[None, None]:
    """Return pooled connections before the shared loop closes."""
    yield
    await engine.dispose()
    await _admin_engine.dispose()


@pytest.fixture
async def admin_session() -> AsyncGenerator[AsyncSession, None]:
    async with AdminSessionLocal() as session:
        yield session


# allow-hardcode: fixed SQL DDL/DML teardown statements (human-written schema
# knowledge -- FK-safe delete order), not a scoring/matching oracle.
_CLEANUP_STATEMENTS = (
    "DELETE FROM opportunity_shares WHERE tenant_id = :t",
    "DELETE FROM client_collaborators WHERE tenant_id = :t",
    "DELETE FROM client_contacts WHERE tenant_id = :t",
    "DELETE FROM client_mentions WHERE tenant_id = :t",
    # `ck_clients_merged_has_target` forbids status='merged' with a null
    # target, so clear both together rather than orphaning a merged row.
    "UPDATE clients SET merged_into_client_id = NULL, status = 'unconfirmed' "
    "WHERE tenant_id = :t",
    "DELETE FROM clients WHERE tenant_id = :t",
    "DELETE FROM email_messages WHERE tenant_id = :t",
    "DELETE FROM mailboxes WHERE tenant_id = :t",
    "DELETE FROM users WHERE tenant_id = :t",
    "DELETE FROM tenants WHERE id = :t",
)


async def cleanup_tenant(*tenant_ids: uuid.UUID) -> None:
    """Delete every row a fixture may have seeded for `tenant_ids`, FK-safely.

    Each statement gets its own session/transaction, so a failure partway
    through (an unexpected constraint, a row already gone) does not abort the
    rest -- a teardown that raises halfway must not leave every later table's
    debris behind.
    """
    for tenant_id in tenant_ids:
        for statement in _CLEANUP_STATEMENTS:
            try:
                async with AdminSessionLocal() as session:
                    await session.execute(text(statement), {"t": tenant_id})
                    await session.commit()
            except Exception:
                pass


async def seed_tenant_with_user(role: str = "recruiter") -> tuple[uuid.UUID, uuid.UUID]:
    """One tenant and one user in it. Returns (tenant_id, user_id).

    Uses the admin session because creating the tenant is what makes the
    RLS-scoped session possible in the first place.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with AdminSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug) "
                "VALUES (:id, :name, :slug)"
            ),
            {"id": tenant_id, "name": f"Agency {tenant_id.hex[:8]}", "slug": tenant_id.hex[:12]},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:id, :tenant_id, :email, :role)"
            ),
            {
                "id": user_id,
                "tenant_id": tenant_id,
                "email": f"{user_id.hex[:8]}@example.test",
                "role": role,
            },
        )
        await session.commit()
    return tenant_id, user_id


@pytest.fixture
async def client() -> httpx.AsyncClient:
    """ASGI transport, not TestClient: TestClient drives its own event loop and
    the engine in app.db.session is pinned to the session-scoped one."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as c:
        yield c


# Fixed rather than `datetime.now()`: an opportunity fixture whose received
# time depended on when the suite ran would drift relative to sort-order
# assertions built around it.
NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


# allow-hardcode: fixed SQL DML statements for test fixture setup (human-written
# schema knowledge -- column lists and literal values), not a scoring/matching
# oracle.
@pytest.fixture
async def seeded():
    """Two agencies, each with one mailbox, and a factory for their vacancies.

    Seeded through the admin role because RLS is the thing under test: fixtures
    written through the restricted role would prove isolation by never having
    inserted the other tenant's rows in the first place.
    """
    tenants: list[uuid.UUID] = []

    async def make_tenant(slug: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        tenant_id, user_id, mailbox_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with AdminSessionLocal() as s:
            await s.execute(
                text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
                {"i": tenant_id, "n": slug, "s": f"{slug}-{tenant_id.hex[:8]}"},
            )
            # The ORM here, not raw SQL: `users.role` is NOT NULL with a
            # Python-side default, which a hand-written INSERT never fires —
            # and naming a role literally would freeze this fixture to a value
            # the model owns.
            s.add(User(id=user_id, tenant_id=tenant_id, email=f"{tenant_id.hex[:8]}@{slug}.sg"))
            # Flushed before the mailbox INSERT: raw SQL does not autoflush, so
            # without this the FK sees a user that has not been written yet.
            await s.flush()
            await s.execute(
                text(
                    "INSERT INTO mailboxes"
                    " (id, tenant_id, user_id, ms_user_id, scope, folder_id, retention_months)"
                    " VALUES (:i, :t, :u, :m, 'user', 'inbox', :r)"
                ),
                {
                    "i": mailbox_id,
                    "t": tenant_id,
                    "u": user_id,
                    "m": f"oid-{tenant_id.hex[:8]}",
                    "r": settings.DEFAULT_RETENTION_MONTHS,
                },
            )
            await s.commit()
        tenants.append(tenant_id)
        return tenant_id, user_id, mailbox_id

    async def make_opportunity(
        tenant_id: uuid.UUID, mailbox_id: uuid.UUID, **fields
    ) -> uuid.UUID:
        email_id, opportunity_id = uuid.uuid4(), uuid.uuid4()
        received = fields.pop("received_datetime", NOW)
        async with AdminSessionLocal() as s:
            await s.execute(
                text(
                    "INSERT INTO email_messages"
                    " (id, tenant_id, mailbox_id, graph_message_id,"
                    " internet_message_id, received_datetime)"
                    " VALUES (:i, :t, :m, :g, :n, :r)"
                ),
                {
                    "i": email_id,
                    "t": tenant_id,
                    "m": mailbox_id,
                    "g": f"graph-{email_id.hex}",
                    "n": f"<{email_id.hex}@example.sg>",
                    "r": received,
                },
            )
            # The ORM again, for `review_status` and `quality_state`: both are
            # NOT NULL with Python-side defaults, and a fixture that named them
            # would be asserting against values it had chosen itself.
            s.add(
                Opportunity(
                    id=opportunity_id,
                    tenant_id=tenant_id,
                    email_message_id=email_id,
                    received_datetime=received,
                    **fields,
                )
            )
            await s.commit()
        return opportunity_id

    async def make_evidence(
        tenant_id: uuid.UUID, opportunity_id: uuid.UUID, *, valid: int, invalid: int
    ) -> None:
        """`valid` verified evidence rows and `invalid` unverified ones.

        The extraction row is looked up from the opportunity rather than passed
        in, so a test can say "this vacancy has 2 of 3 fields verified" without
        also having to know about the email it came from — which is exactly the
        detail the endpoint is supposed to be hiding.
        """
        async with AdminSessionLocal() as s:
            email_id = (
                await s.execute(
                    text("SELECT email_message_id FROM opportunities WHERE id = :i"),
                    {"i": opportunity_id},
                )
            ).scalar_one()
            extraction_id = uuid.uuid4()
            s.add(
                Extraction(
                    id=extraction_id,
                    tenant_id=tenant_id,
                    email_message_id=email_id,
                    model_name="test-model",
                    prompt_version="v0",
                )
            )
            await s.flush()
            for n in range(valid + invalid):
                s.add(
                    ExtractionEvidence(
                        tenant_id=tenant_id,
                        extraction_id=extraction_id,
                        opportunity_id=opportunity_id,
                        field_name=f"field_{n}",
                        # A real confidence, so a payload that leaked it would
                        # be caught rather than passing on a column of nulls.
                        model_confidence=0.42,
                        evidence_valid=n < valid,
                    )
                )
            await s.commit()

    yield make_tenant, make_opportunity, make_evidence

    for tid in tenants:
        async with tenant_session(tid) as s:
            await s.execute(text("DELETE FROM opportunities"))
            await s.execute(text("DELETE FROM email_messages"))
            await s.execute(text("DELETE FROM mailboxes"))
            await s.execute(text("DELETE FROM users"))
            await s.execute(text("DELETE FROM tenants"))


def sign_in(client: httpx.AsyncClient, user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """The cookie the OAuth callback would have set, without the OAuth."""
    client.cookies.set(
        SESSION_COOKIE,
        _session_serializer.dumps({"uid": str(user_id), "tid": str(tenant_id)}),
    )


async def make_candidate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID | None = None,
    **fields: object,
) -> uuid.UUID:
    """Insert a candidate through the ORM, not raw SQL.

    `pipeline_stage`, `record_status` and `users.role` are NOT NULL with
    PYTHON-side defaults, which a hand-written INSERT never fires. The same
    trap `make_tenant` documents.
    """
    candidate_id = uuid.uuid4()
    session.add(
        Candidate(
            id=candidate_id,
            tenant_id=tenant_id,
            full_name=fields.pop("full_name", "Wei Ming Tan"),
            owner_id=owner_id,
            **fields,
        )
    )
    await session.flush()
    return candidate_id


async def make_user(
    session: AsyncSession, tenant_id: uuid.UUID, email: str, role: str = "recruiter"
) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, tenant_id=tenant_id, email=email, role=role))
    await session.flush()
    return user_id


@pytest.fixture
async def run_import():
    """Drive a real import end to end. There is no existing helper for this —
    the import tests build their own rows inline, which is why Task 13 needs
    one that can vary `uploaded_by`.

    `apply_import` takes the session, so the import row and the call share one
    transaction. A second session would let the import run against a
    `candidate_imports` row that had not committed.
    """
    import datetime as dt

    from app.models.candidate import CandidateImport
    from app.services.imports.apply import apply_import
    from app.services.imports.rows import CandidateRecord

    async def _run(tenant_id, uploaded_by, rows):
        async with AdminSessionLocal() as session:
            import_id = uuid.uuid4()
            session.add(
                CandidateImport(
                    id=import_id,
                    tenant_id=tenant_id,
                    uploaded_by=uploaded_by,
                    # All four are NOT NULL with no default (candidate.py:707-710).
                    # The values are irrelevant to what these tests assert; their
                    # presence is not.
                    filename="test.xlsx",
                    content_type="application/vnd.ms-excel",
                    byte_size=1,
                    object_key=f"test/{import_id}",
                )
            )
            await session.flush()
            outcome = await apply_import(
                session,
                tenant_id=tenant_id,
                import_id=import_id,
                # `CandidateRecord` has no defaults — every field is required —
                # so a test naming only the columns it cares about would fail
                # on the constructor rather than on what it set out to assert.
                candidates=[
                    CandidateRecord(
                        **{
                            "line": line,
                            "full_name": "",
                            "email": None,
                            "phone_raw": None,
                            "phone_e164": None,
                            "current_title": None,
                            "current_employer": None,
                            "location": None,
                            **row,
                        }
                    )
                    for line, row in enumerate(rows, start=2)
                ],
                roles=[],
                today=dt.date(2026, 7, 31),
            )
            # `apply_import` never commits — the worker owns that (§
            # `import_jobs.py`). Without it here the rows exist only inside
            # this session, and a test that checks them through `admin_session`
            # sees nothing.
            await session.commit()
            return outcome

    return _run
