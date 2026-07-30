"""The constraint every composite user foreign key in this feature needs.

Without it a share row, a client assignee or an opportunity assignee could
name a user in another agency, and the tenant boundary would hold only in
application code.
"""

from sqlalchemy import text

from tests.conftest import AdminSessionLocal


async def test_users_has_tenant_id_id_unique_constraint() -> None:
    async with AdminSessionLocal() as session:
        found = (
            await session.execute(
                text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'uq_users_tenant_id_id' "
                    "AND conrelid = 'users'::regclass"
                )
            )
        ).scalar_one_or_none()

    assert found == 1, "uq_users_tenant_id_id is missing; composite FKs cannot reference users"
