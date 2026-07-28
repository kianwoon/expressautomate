"""Somebody has to be able to delete a candidate.

`role` has existed since the first migration and has never been read. Phase 1
starts reading it, which turns a dormant column into an access control — so the
values it can hold, and who gets which, stop being cosmetic.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import tenant_session
from tests.conftest import AdminSessionLocal
from tests.test_auth import client, fake_msal, microsoft_configured, sign_in, token_response

__all__ = ["client", "fake_msal", "microsoft_configured"]


@pytest.fixture
async def agency():
    tid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.commit()
    yield tid
    async with AdminSessionLocal() as s:
        await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


async def test_role_rejects_a_value_nobody_checks_for(agency) -> None:
    """A typo in a column that gates deletion must fail loudly, not silently."""
    with pytest.raises(IntegrityError):
        async with tenant_session(agency) as s:
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, role) "
                    "VALUES (:i, :t, 'x@a.sg', 'administrator')"
                ),
                {"i": uuid.uuid4(), "t": agency},
            )
            await s.commit()


async def test_owner_and_recruiter_are_both_accepted(agency) -> None:
    async with tenant_session(agency) as s:
        for role in ("owner", "recruiter"):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, role) "
                    "VALUES (:i, :t, :e, :r)"
                ),
                {"i": uuid.uuid4(), "t": agency, "e": f"{role}@a.sg", "r": role},
            )
        await s.commit()
    async with tenant_session(agency) as s:
        roles = sorted((await s.execute(text("SELECT role FROM users"))).scalars().all())
    assert roles == ["owner", "recruiter"]


async def _sign_in_new_user(client, monkeypatch, agency: uuid.UUID, oid: str) -> uuid.UUID:
    """Drive the real sign-in path for a work account in `agency`.

    `agency`'s tid IS its tenant id for a work account (`_tenant_for`), so
    signing in with `tid=str(agency)` lands the new user in the tenant the
    `agency` fixture already created.
    """
    response = await sign_in(
        client, monkeypatch, token_response(str(agency), oid, f"{oid}@a.sg")
    )
    assert response.status_code == 307
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text("SELECT id FROM users WHERE tenant_id = :t AND ms_object_id = :o"),
                {"t": agency, "o": oid},
            )
        ).scalar_one()


async def test_the_first_user_of_a_tenant_becomes_the_owner(
    agency, client, monkeypatch
) -> None:
    """Follow the real sign-in path — do not insert users directly here.

    Build the two calls the way tests/test_auth.py already drives a sign-in,
    reusing its helpers rather than writing a second definition of the flow.
    """
    first = await _sign_in_new_user(client, monkeypatch, agency, oid="oid-first")
    second = await _sign_in_new_user(client, monkeypatch, agency, oid="oid-second")

    async with tenant_session(agency) as s:
        roles = dict(
            (await s.execute(text("SELECT id, role FROM users"))).all()
        )
    assert roles[first] == "owner"
    assert roles[second] == "recruiter"


async def test_the_backfill_promotes_only_the_earliest_user(agency) -> None:
    """The spec requires the backfill migration give every existing tenant
    exactly one owner. Invoking the migration module directly would run
    alembic's own env/transaction machinery outside this test's control, so
    instead this seeds users with known `created_at` ordering (all starting
    as `recruiter`, mirroring a tenant that predates the `owner` role) and
    then runs the exact `UPDATE ... WHERE id IN (SELECT DISTINCT ON ...)`
    statement `alembic/versions/20260728_1500_owner_role.py`'s `upgrade()`
    uses for the promotion. Asserting against that literal SQL is what
    actually exercises the backfill's logic, not just its intent.
    """
    ids = [uuid.uuid4() for _ in range(4)]
    async with AdminSessionLocal() as s:
        for i, uid in enumerate(ids):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, role, created_at) "
                    "VALUES (:i, :t, :e, 'recruiter', now() - make_interval(secs => :age))"
                ),
                {"i": uid, "t": agency, "e": f"user{i}@a.sg", "age": (4 - i) * 60},
            )
        await s.commit()

    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                """
                UPDATE users SET role = 'owner'
                WHERE id IN (
                    SELECT DISTINCT ON (tenant_id) id
                    FROM users
                    WHERE NOT EXISTS (
                        SELECT 1 FROM users u2
                        WHERE u2.tenant_id = users.tenant_id AND u2.role = 'owner'
                    )
                    ORDER BY tenant_id, created_at ASC, id ASC
                )
                """
            )
        )
        await s.commit()

    async with tenant_session(agency) as s:
        roles = dict((await s.execute(text("SELECT id, role FROM users"))).all())

    owners = [uid for uid, role in roles.items() if role == "owner"]
    assert owners == [ids[0]]
    assert all(roles[uid] == "recruiter" for uid in ids[1:])


async def test_an_owner_signing_in_again_is_not_demoted(agency, client, monkeypatch) -> None:
    uid = await _sign_in_new_user(client, monkeypatch, agency, oid="oid-first")
    await _sign_in_new_user(client, monkeypatch, agency, oid="oid-second")
    await _sign_in_new_user(client, monkeypatch, agency, oid="oid-first")  # same person returns

    async with tenant_session(agency) as s:
        role = (
            await s.execute(text("SELECT role FROM users WHERE id = :i"), {"i": uid})
        ).scalar_one()
    assert role == "owner"
