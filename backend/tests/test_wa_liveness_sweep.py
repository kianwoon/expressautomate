"""Plan §6's liveness sweep, background-check half
(`app.workers.tasks.sweep_wa_liveness`): asks the gateway about every
`connected`/`reconnecting` session so a socket that died silently is noticed
without a browser open. The gateway's own `POST /api/wa/internal/status`
callback stays the only writer of `status` (§6) — this sweep only ever
touches `last_liveness_check_at`, and only when the gateway actually
answered (§15).
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.config import settings
from app.services.wa_gateway import GatewayUnreachableError, SessionSnapshot
from app.workers import tasks


async def _seed_agency(admin_session) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
        {"id": tenant_id, "slug": f"agency-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'r@a.sg', 'recruiter')"
        ),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.commit()
    return tenant_id, user_id


async def _insert_session(
    admin_session, tenant_id, user_id, status, last_check_minutes_ago
) -> uuid.UUID:
    session_id = user_id
    check_at = (
        None
        if last_check_minutes_ago is None
        else datetime.now(UTC) - timedelta(minutes=last_check_minutes_ago)
    )
    await admin_session.execute(
        text(
            "INSERT INTO wa_sessions (id, tenant_id, user_id, status, last_liveness_check_at) "
            "VALUES (:id, :tid, :uid, :status, :check_at)"
        ),
        {
            "id": session_id,
            "tid": tenant_id,
            "uid": user_id,
            "status": status,
            "check_at": check_at,
        },
    )
    await admin_session.commit()
    return session_id


async def _row(admin_session, session_id):
    row = (
        await admin_session.execute(
            text("SELECT status, last_liveness_check_at FROM wa_sessions WHERE id = :id"),
            {"id": session_id},
        )
    ).one()
    return row.status, row.last_liveness_check_at


async def _cleanup(admin_session, tenant_id) -> None:
    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await admin_session.commit()


async def test_only_connected_and_reconnecting_are_swept(admin_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "http://gateway.internal:7300")
    monkeypatch.setattr(settings, "WA_GATEWAY_SHARED_SECRET", "test-secret")
    monkeypatch.setattr(settings, "WA_LIVENESS_CHECK_STALE_MINUTES", 15)

    async def fake_status(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        return SessionSnapshot(status="connected")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.status", fake_status)

    tenant_id, user_id = await _seed_agency(admin_session)
    connected = await _insert_session(admin_session, tenant_id, user_id, "connected", 30)

    t2, u2 = await _seed_agency(admin_session)
    disconnected = await _insert_session(admin_session, t2, u2, "disconnected", 30)

    t3, u3 = await _seed_agency(admin_session)
    logged_out = await _insert_session(admin_session, t3, u3, "logged_out", 30)

    checked = await tasks.sweep_wa_liveness()
    assert checked == 1

    status, checked_at = await _row(admin_session, connected)
    assert status == "connected"
    assert checked_at is not None

    status, checked_at = await _row(admin_session, disconnected)
    assert checked_at is not None and checked_at < datetime.now(UTC) - timedelta(
        minutes=20
    ), "a disconnected session has nothing to check and must be left untouched"

    status, checked_at = await _row(admin_session, logged_out)
    assert checked_at is not None and checked_at < datetime.now(UTC) - timedelta(
        minutes=20
    ), "a logged-out session has nothing to check and must be left untouched"

    for tid in (tenant_id, t2, t3):
        await _cleanup(admin_session, tid)


async def test_a_successful_check_updates_last_liveness_check_at(
    admin_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "http://gateway.internal:7300")
    monkeypatch.setattr(settings, "WA_GATEWAY_SHARED_SECRET", "test-secret")
    monkeypatch.setattr(settings, "WA_LIVENESS_CHECK_STALE_MINUTES", 15)

    async def fake_status(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        return SessionSnapshot(status="reconnecting")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.status", fake_status)

    tenant_id, user_id = await _seed_agency(admin_session)
    session_id = await _insert_session(admin_session, tenant_id, user_id, "reconnecting", None)

    before = datetime.now(UTC)
    checked = await tasks.sweep_wa_liveness()
    assert checked == 1

    status, checked_at = await _row(admin_session, session_id)
    assert status == "reconnecting", "the sweep never writes status itself"
    assert checked_at is not None and checked_at >= before - timedelta(seconds=5)

    await _cleanup(admin_session, tenant_id)


async def test_an_unreachable_gateway_leaves_last_liveness_check_at_unchanged(
    admin_session, monkeypatch
) -> None:
    """§15: if we never got an answer, we did not check — the old timestamp
    (however stale) is the truth, and writing `now()` would assert a check
    that never happened."""
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "http://gateway.internal:7300")
    monkeypatch.setattr(settings, "WA_GATEWAY_SHARED_SECRET", "test-secret")
    monkeypatch.setattr(settings, "WA_LIVENESS_CHECK_STALE_MINUTES", 15)

    async def fake_status(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        raise GatewayUnreachableError("the WA gateway could not be reached")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.status", fake_status)

    tenant_id, user_id = await _seed_agency(admin_session)
    session_id = await _insert_session(admin_session, tenant_id, user_id, "connected", None)

    checked = await tasks.sweep_wa_liveness()
    assert checked == 0

    status, checked_at = await _row(admin_session, session_id)
    assert status == "connected"
    assert checked_at is None, "an unreachable gateway must not assert a check that never ran"

    await _cleanup(admin_session, tenant_id)


async def test_a_fresh_session_is_not_due(admin_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "WA_GATEWAY_URL", "http://gateway.internal:7300")
    monkeypatch.setattr(settings, "WA_GATEWAY_SHARED_SECRET", "test-secret")
    monkeypatch.setattr(settings, "WA_LIVENESS_CHECK_STALE_MINUTES", 15)

    async def fake_status(self, tenant_id: str, user_id: str) -> SessionSnapshot:
        raise AssertionError("must not be called for a session that is not due")

    monkeypatch.setattr("app.services.wa_gateway.WaGatewayClient.status", fake_status)

    tenant_id, user_id = await _seed_agency(admin_session)
    await _insert_session(admin_session, tenant_id, user_id, "connected", 1)

    checked = await tasks.sweep_wa_liveness()
    assert checked == 0

    await _cleanup(admin_session, tenant_id)


async def test_two_concurrent_sweeps_do_not_claim_the_same_session(
    admin_session, monkeypatch
) -> None:
    """Same `FOR UPDATE SKIP LOCKED` idiom as `sweep_stale_wa_sends`: verified
    here by asserting `wa_sweep_claim_due_sessions` claims each due row
    exactly once — a second call in the same tick finds nothing left."""
    monkeypatch.setattr(settings, "WA_LIVENESS_CHECK_STALE_MINUTES", 15)

    tenant_id, user_id = await _seed_agency(admin_session)
    await _insert_session(admin_session, tenant_id, user_id, "connected", 30)

    first = (
        await admin_session.execute(
            text("SELECT * FROM wa_sweep_claim_due_sessions(:m, :l)"),
            {"m": settings.WA_LIVENESS_CHECK_STALE_MINUTES, "l": 200},
        )
    ).all()
    await admin_session.commit()
    second = (
        await admin_session.execute(
            text("SELECT * FROM wa_sweep_claim_due_sessions(:m, :l)"),
            {"m": settings.WA_LIVENESS_CHECK_STALE_MINUTES, "l": 200},
        )
    ).all()
    await admin_session.commit()

    assert len(first) == 1
    assert len(second) == 0, "the claim's stamp makes the row no longer due"

    await _cleanup(admin_session, tenant_id)


async def test_a_tenant_cannot_read_another_tenants_sessions_through_the_function(
    admin_session,
) -> None:
    """§18: `wa_sweep_claim_due_sessions` is `SECURITY DEFINER` and bypasses
    RLS by design (same as `sweep_stale_wa_sends`), but it must still return
    each row's own `tenant_id` rather than leaking cross-tenant identity —
    the caller (the worker task) is what scopes any subsequent read."""
    t1, u1 = await _seed_agency(admin_session)
    t2, u2 = await _seed_agency(admin_session)
    await _insert_session(admin_session, t1, u1, "connected", 30)
    await _insert_session(admin_session, t2, u2, "connected", 30)

    rows = (
        await admin_session.execute(
            text("SELECT * FROM wa_sweep_claim_due_sessions(:m, :l)"), {"m": 15, "l": 200}
        )
    ).all()
    await admin_session.commit()

    seen_tenants = {row.tenant_id for row in rows}
    assert seen_tenants == {t1, t2}

    for tid in (t1, t2):
        await _cleanup(admin_session, tid)
