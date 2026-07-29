"""P5's liveness sweep, pending-row half (plan §6): a `pending`
`candidate_activities` row this old was left by a process that died before it
could resolve the row, so it becomes `unknown` — never `failed`, because §15
forbids claiming a refusal nobody observed. A row still comfortably inside
the gateway's own timeout must be left alone: it may just be slow.

allow-hardcode: the SQL below is test fixture data, the same idiom
`test_notify_sweep.py` and `test_candidate_whatsapp_send.py` use — written out
rather than built through the ORM so a schema change breaks these tests
loudly. It is fixture setup, not a phrase list matched against anything.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.config import settings
from app.workers import tasks


async def _insert(
    admin_session, tenant_id, candidate_id, user_id, status, age_minutes
) -> uuid.UUID:
    row_id = uuid.uuid4()
    ts = datetime.now(UTC) - timedelta(minutes=age_minutes)
    await admin_session.execute(
        text(
            "INSERT INTO candidate_activities "
            "(id, tenant_id, candidate_id, user_id, activity_type, channel, status, "
            "client_request_id, created_at, updated_at) "
            "VALUES (:id, :tid, :cid, :uid, 'whatsapp_sent', 'whatsapp', :status, :key, "
            ":ts, :ts)"
        ),
        {
            "id": row_id,
            "tid": tenant_id,
            "cid": candidate_id,
            "uid": user_id,
            "status": status,
            "key": uuid.uuid4(),
            "ts": ts,
        },
    )
    await admin_session.commit()
    return row_id


async def _status(admin_session, row_id) -> tuple[str, str | None]:
    row = (
        await admin_session.execute(
            text("SELECT status, error FROM candidate_activities WHERE id = :id"),
            {"id": row_id},
        )
    ).one()
    return row.status, row.error


async def _seed_agency(admin_session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, user_id, candidate_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
        {"id": tenant_id, "slug": f"agency-{tenant_id}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'r@a.sg', 'recruiter')"
        ),
        {"id": user_id, "tid": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO candidates (id, tenant_id, full_name, phone_e164) "
            "VALUES (:id, :tid, 'Tan Wei Ling', '+6591234567')"
        ),
        {"id": candidate_id, "tid": tenant_id},
    )
    await admin_session.commit()
    return tenant_id, user_id, candidate_id


async def test_a_stale_pending_row_becomes_unknown_never_failed(
    admin_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "WA_SEND_STALE_PENDING_MINUTES", 10)
    tenant_id, user_id, candidate_id = await _seed_agency(admin_session)

    stale = await _insert(admin_session, tenant_id, candidate_id, user_id, "pending", 30)
    fresh = await _insert(admin_session, tenant_id, candidate_id, user_id, "pending", 1)

    swept = await tasks.sweep_stale_wa_sends()
    assert swept == 1

    status, error = await _status(admin_session, stale)
    assert status == "unknown"
    assert status != "failed"
    assert error and "do not know" in error

    status, _ = await _status(admin_session, fresh)
    assert status == "pending", "a send merely slow, not dead, must be left alone"

    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await admin_session.commit()


async def test_the_sweep_never_touches_a_row_already_settled(admin_session, monkeypatch) -> None:
    """A `sent` or `failed` row is settled; the sweep's `WHERE status =
    'pending'` must never revisit it, no matter how old."""
    monkeypatch.setattr(settings, "WA_SEND_STALE_PENDING_MINUTES", 10)
    tenant_id, user_id, candidate_id = await _seed_agency(admin_session)

    sent = await _insert(admin_session, tenant_id, candidate_id, user_id, "sent", 999)

    swept = await tasks.sweep_stale_wa_sends()
    assert swept == 0

    status, _ = await _status(admin_session, sent)
    assert status == "sent"

    await admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
    await admin_session.commit()
