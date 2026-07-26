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

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import engine

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
