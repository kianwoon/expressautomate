"""Schema guarantees the RLS policy would otherwise hide behind empty results.

These use `admin_session` (the schema owner, which bypasses RLS) because a
constraint violation and a policy-filtered read are indistinguishable from the
application role.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.notification import address_digest


def test_address_digest_is_stable_and_not_reversible() -> None:
    first = address_digest("+6591234567")
    assert first == address_digest("+6591234567")
    assert first != address_digest("+6591234568")
    assert "6591234567" not in first


@pytest.fixture
async def tenant_pair(admin_session):
    a, b = uuid.uuid4(), uuid.uuid4()
    for tid, name in ((a, "agency-a"), (b, "agency-b")):
        await admin_session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tid, "name": name, "slug": f"{name}-{tid}"},
        )
    await admin_session.commit()
    yield a, b
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [a, b]}
    )
    await admin_session.commit()


async def test_same_address_may_exist_in_two_tenants(admin_session, tenant_pair) -> None:
    """One recruiter can work for two agencies. A global unique index would
    make the second link fail with nothing to explain it."""
    a, b = tenant_pair
    digest = address_digest("+6591234567")
    for tid in (a, b):
        await admin_session.execute(
            text(
                "INSERT INTO notification_destinations "
                "(id, tenant_id, channel, address_encrypted, address_hash) "
                "VALUES (:id, :tid, 'whatsapp', 'ciphertext', :hash)"
            ),
            {"id": uuid.uuid4(), "tid": tid, "hash": digest},
        )
    await admin_session.commit()


async def test_same_address_twice_in_one_tenant_is_rejected(
    admin_session, tenant_pair
) -> None:
    a, _ = tenant_pair
    digest = address_digest("+6599999999")
    insert = text(
        "INSERT INTO notification_destinations "
        "(id, tenant_id, channel, address_encrypted, address_hash) "
        "VALUES (:id, :tid, 'whatsapp', 'ciphertext', :hash)"
    )
    await admin_session.execute(insert, {"id": uuid.uuid4(), "tid": a, "hash": digest})
    await admin_session.commit()
    # The constraint is not deferrable, so Postgres raises on the second
    # INSERT's own statement execution rather than waiting for commit.
    with pytest.raises(IntegrityError):
        await admin_session.execute(
            insert, {"id": uuid.uuid4(), "tid": a, "hash": digest}
        )
    # The failed statement leaves the session's transaction aborted; the
    # `tenant_pair` fixture's teardown reuses this same session to delete the
    # tenants it created, and that DELETE would itself raise
    # `InFailedSqlTransactionError` without this rollback first.
    await admin_session.rollback()


async def test_suppressions_table_is_readable_and_forced(admin_session) -> None:
    """Not tenant-scoped, but still FORCE RLS — `verify_rls_enforced` refuses
    to boot otherwise, and the send path must be able to read it."""
    row = (
        await admin_session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'whatsapp_suppressions'"
            )
        )
    ).one()
    assert row == (True, True)
