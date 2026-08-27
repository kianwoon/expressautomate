"""Row-level security on the WA gateway session tables (plan §3, §18).

`wa_session_keys` holds every recruiter's WhatsApp signal keys. The values are
encrypted with a key only the gateway service has, but encryption is the second
line: agency B must not be able to *read the rows at all*. These tests prove
the policy, in the same shape as `test_rls.py` and running as the same
restricted role.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.rls import tenant_session
from app.db.session import SessionLocal

# The two-agencies fixture clears every wa_sessions row (not per-tenant) in
# teardown; run concurrently that collides with other files' WhatsApp writes.
# Same global-state class f48cc82 serializes — run serially in CI.
pytestmark = pytest.mark.serial


async def _seed(tenant_id: uuid.UUID, slug: str, email: str) -> uuid.UUID:
    """A tenant, a recruiter, a session and one stored key. Returns session id."""
    session_id = uuid.uuid4()
    async with tenant_session(tenant_id) as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :s)"),
            {"i": tenant_id, "n": slug, "s": slug},
        )
        user_id = uuid.uuid4()
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'recruiter')"),
            {"i": user_id, "t": tenant_id, "e": email},
        )
        await s.execute(
            text(
                "INSERT INTO wa_sessions (id, tenant_id, user_id, status) "
                "VALUES (:i, :t, :u, 'connected')"
            ),
            {"i": session_id, "t": tenant_id, "u": user_id},
        )
        await s.execute(
            text(
                "INSERT INTO wa_session_keys "
                "(tenant_id, session_id, category, key_id, value_encrypted) "
                "VALUES (:t, :s, 'creds', '', :v)"
            ),
            # Not real ciphertext — this file tests the policy, not the crypto.
            # The crypto lives in gateway/src/crypto.ts and is tested there.
            {"t": tenant_id, "s": session_id, "v": slug.encode()},
        )
    return session_id


@pytest.fixture
async def two_agencies() -> tuple[tuple[uuid.UUID, uuid.UUID], tuple[uuid.UUID, uuid.UUID]]:
    a, b = uuid.uuid4(), uuid.uuid4()
    a_session = await _seed(a, f"wa-agency-a-{a.hex[:6]}", "a@wa-agency-a.sg")
    b_session = await _seed(b, f"wa-agency-b-{b.hex[:6]}", "b@wa-agency-b.sg")
    yield (a, a_session), (b, b_session)
    for tid in (a, b):
        async with tenant_session(tid) as s:
            await s.execute(text("DELETE FROM wa_session_keys"))
            await s.execute(text("DELETE FROM wa_sessions"))
            await s.execute(text("DELETE FROM users"))
            await s.execute(text("DELETE FROM tenants"))


async def test_agency_b_cannot_read_agency_a_sessions(two_agencies) -> None:
    (a, a_session), (b, b_session) = two_agencies

    async with tenant_session(a) as s:
        ids = (await s.execute(text("SELECT id FROM wa_sessions"))).scalars().all()
    assert ids == [a_session]

    async with tenant_session(b) as s:
        ids = (await s.execute(text("SELECT id FROM wa_sessions"))).scalars().all()
    assert ids == [b_session]


async def test_agency_b_cannot_read_agency_a_keys(two_agencies) -> None:
    """The one that matters: these rows are WhatsApp identities."""
    (a, a_session), (b, _) = two_agencies

    async with tenant_session(b) as s:
        rows = (
            await s.execute(
                text("SELECT value_encrypted FROM wa_session_keys WHERE session_id = :s"),
                {"s": a_session},
            )
        ).all()
    assert rows == [], "agency B read agency A's signal keys"

    async with tenant_session(a) as s:
        count = (await s.execute(text("SELECT count(*) FROM wa_session_keys"))).scalar_one()
    assert count == 1


async def test_unscoped_session_sees_no_sessions_or_keys(two_agencies) -> None:
    """Fail closed: forgetting to scope yields nothing, not everything."""
    async with SessionLocal() as s:
        assert (await s.execute(text("SELECT count(*) FROM wa_sessions"))).scalar_one() == 0
        assert (await s.execute(text("SELECT count(*) FROM wa_session_keys"))).scalar_one() == 0


async def test_cannot_write_a_key_row_for_another_tenant(two_agencies) -> None:
    """WITH CHECK stops B forging A's tenant_id onto a key row."""
    (a, a_session), (b, _) = two_agencies
    with pytest.raises(DBAPIError):
        async with tenant_session(b) as s:
            await s.execute(
                text(
                    "INSERT INTO wa_session_keys "
                    "(tenant_id, session_id, category, key_id, value_encrypted) "
                    "VALUES (:t, :s, 'session', 'x', :v)"
                ),
                {"t": a, "s": a_session, "v": b"forged"},
            )


async def test_b_cannot_delete_a_keys(two_agencies) -> None:
    """A delete that matches nothing is the policy working, not a no-op bug."""
    (a, a_session), (b, _) = two_agencies
    async with tenant_session(b) as s:
        result = await s.execute(
            text("DELETE FROM wa_session_keys WHERE session_id = :s"), {"s": a_session}
        )
        assert result.rowcount == 0

    async with tenant_session(a) as s:
        count = (await s.execute(text("SELECT count(*) FROM wa_session_keys"))).scalar_one()
    assert count == 1


async def test_one_session_per_user(two_agencies) -> None:
    """Two rows for one recruiter would let two sockets fight over one device."""
    (a, _), _ = two_agencies
    async with tenant_session(a) as s:
        user_id = (await s.execute(text("SELECT user_id FROM wa_sessions"))).scalar_one()

    with pytest.raises(DBAPIError):
        async with tenant_session(a) as s:
            await s.execute(
                text("INSERT INTO wa_sessions (id, tenant_id, user_id) VALUES (:i, :t, :u)"),
                {"i": uuid.uuid4(), "t": a, "u": user_id},
            )


async def test_status_vocabulary_is_constrained(two_agencies) -> None:
    """§15: the UI switches on this, so an unknown status must not reach it."""
    (a, a_session), _ = two_agencies
    with pytest.raises(DBAPIError):
        async with tenant_session(a) as s:
            await s.execute(
                text("UPDATE wa_sessions SET status = 'vibing' WHERE id = :i"),
                {"i": a_session},
            )


async def test_deleting_a_session_takes_its_keys(two_agencies) -> None:
    """Orphaned key rows would be undecryptable material kept for no reason."""
    (a, a_session), _ = two_agencies
    async with tenant_session(a) as s:
        await s.execute(text("DELETE FROM wa_sessions WHERE id = :i"), {"i": a_session})
        count = (await s.execute(text("SELECT count(*) FROM wa_session_keys"))).scalar_one()
    assert count == 0
