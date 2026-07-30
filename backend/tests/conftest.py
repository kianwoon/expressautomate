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
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import engine

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
