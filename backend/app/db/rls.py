"""Row-level security helpers (plan §18, §30).

Isolation is enforced in the database, not in application code. Every
tenant-scoped table carries an RLS policy comparing `tenant_id` against the
`app.tenant_id` setting on the current connection; a request that forgets to
set it sees **zero rows** rather than everything, so the failure mode is a
visibly empty screen instead of a silent cross-agency leak.

Two conditions make this real, and both are asserted at startup by
`verify_rls_enforced()`:

1. The application connects as a role **without** BYPASSRLS. Koyeb's
   `koyeb-adm` has it, so a separate non-owner role is used at runtime and
   `koyeb-adm` is reserved for migrations.
2. Tables are declared FORCE ROW LEVEL SECURITY, so even a table owner is
   subject to the policy.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.session import SessionLocal

log = get_logger(__name__)

TENANT_SETTING = "app.tenant_id"


@asynccontextmanager
async def tenant_session(tenant_id: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
    """A session scoped to one tenant for the life of its transaction.

    `set_config(..., is_local => true)` is the parameterised equivalent of
    `SET LOCAL`: the plain `SET` statement takes only literals, so binding the
    tenant id through it is impossible. Being transaction-local, the value is
    discarded on commit or rollback and can never leak to the next checkout of
    a pooled connection.
    """
    async with SessionLocal() as session:
        await session.begin()
        await session.execute(
            text("SELECT set_config(:key, :tid, true)"),
            {"key": TENANT_SETTING, "tid": str(tenant_id)},
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def verify_rls_enforced() -> None:
    """Fail startup if isolation is not actually being enforced.

    A misconfigured role or a table with RLS switched off turns every tenant
    query into a full-table read. That must never boot silently.
    """
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT current_user, "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
                )
            )
        ).one()
        current_user, bypasses = row

        if bypasses:
            raise RuntimeError(
                f"Database role {current_user!r} has BYPASSRLS — tenant isolation would be "
                "unenforced. Point DATABASE_URL at the application role, not the admin role."
            )

        unprotected = (
            await session.execute(
                text(
                    """
                    SELECT c.relname
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relkind = 'r'
                      AND EXISTS (
                          SELECT 1 FROM pg_attribute a
                          WHERE a.attrelid = c.oid
                            AND a.attname = 'tenant_id'
                            AND NOT a.attisdropped
                      )
                      AND NOT (c.relrowsecurity AND c.relforcerowsecurity)
                    ORDER BY c.relname
                    """
                )
            )
        ).scalars().all()

        if unprotected:
            raise RuntimeError(
                "Tables carry tenant_id but lack FORCE ROW LEVEL SECURITY: "
                f"{', '.join(unprotected)}"
            )

    log.info("rls_verified", role=current_user)
