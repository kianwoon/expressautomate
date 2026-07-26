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

from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import engine

# Hosts a test run is allowed to write to. Anything else is assumed to be real.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db"}


def _refuse_to_run_against_a_remote_database() -> None:
    """Abort collection if the configured database is not obviously disposable.

    These tests INSERT and DELETE in `tenants` and `users`, and the schema-level
    ones use the admin role, which bypasses RLS. Pointed at a live database that
    is exactly a data-loss bug — and it already happened once, stranding test
    fixtures in production before this guard existed.
    """
    host = (urlsplit(str(settings.DATABASE_URL)).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return
    raise RuntimeError(
        f"Refusing to run the test suite against database host {host!r}.\n"
        "The suite writes and deletes rows and uses the RLS-bypassing admin role.\n"
        "Point DATABASE_URL and DATABASE_ADMIN_URL at a local or CI Postgres "
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
