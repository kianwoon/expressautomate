"""Shared test fixtures.

Tests run against the real `expressautomate` database and clean up after
themselves; every fixture here exists to keep that honest.
"""

from collections.abc import AsyncGenerator

import pytest

from app.db.session import engine


@pytest.fixture(scope="session", autouse=True)
async def _dispose_engine() -> AsyncGenerator[None, None]:
    """Return pooled connections before the shared loop closes."""
    yield
    await engine.dispose()
