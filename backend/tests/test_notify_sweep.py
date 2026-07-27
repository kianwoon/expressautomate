"""The sweep has three duties: lost enqueues, the rollup's tail, and a worker
killed outright while holding a 'sending' claim."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.notification import CHANNEL_TELEGRAM, address_digest
from app.services.notify.events import EVENT_OPPORTUNITY_NEW
from app.workers import tasks


@pytest.fixture
async def scene(admin_session):
    tenant_id, user_id, dest_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, 'ciphertext', :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "hash": address_digest("12345"),
        },
    )
    await admin_session.commit()
    yield tenant_id, dest_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def _insert(admin_session, tenant_id, dest_id, status, age_minutes) -> uuid.UUID:
    row_id = uuid.uuid4()
    ts = datetime.now(UTC) - timedelta(minutes=age_minutes)
    await admin_session.execute(
        text(
            "INSERT INTO notification_deliveries "
            "(id, tenant_id, destination_id, event_kind, subject_id, status, "
            "created_at, updated_at) "
            "VALUES (:id, :tid, :did, :kind, :sub, :status, :ts, :ts)"
        ),
        {
            "id": row_id,
            "tid": tenant_id,
            "did": dest_id,
            "kind": EVENT_OPPORTUNITY_NEW,
            "sub": uuid.uuid4(),
            "status": status,
            "ts": ts,
        },
    )
    await admin_session.commit()
    return row_id


async def test_a_stale_pending_row_is_requeued(scene, admin_session, monkeypatch) -> None:
    """This is the lost-enqueue net. Without it, 'no notification is lost' is
    simply false — `enqueue` fails soft by design."""
    tenant_id, dest_id = scene
    row_id = await _insert(
        admin_session,
        tenant_id,
        dest_id,
        "pending",
        settings.NOTIFY_DELIVERY_STALE_MINUTES + 5,
    )
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append({"name": name, **kwargs})
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()

    assert any(q["delivery_id"] == str(row_id) for q in queued)
    assert queued[0]["name"] == "deliver_notification"
    assert queued[0]["tenant_id"] == str(tenant_id)


async def test_a_fresh_pending_row_is_left_alone(scene, admin_session, monkeypatch) -> None:
    """Otherwise the sweep competes with a job that is merely slow."""
    tenant_id, dest_id = scene
    await _insert(admin_session, tenant_id, dest_id, "pending", 1)
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()
    assert queued == []


async def test_an_orphaned_suppressed_batch_is_flushed(
    scene, admin_session, monkeypatch
) -> None:
    """The rollup's tail. If no further event ever arrives, '+N more' is lost
    forever without this."""
    tenant_id, dest_id = scene
    await _insert(admin_session, tenant_id, dest_id, "suppressed", 90)
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()
    assert len(queued) == 1


async def test_a_suppressed_row_inside_the_cap_window_is_left_alone(
    scene, admin_session, monkeypatch
) -> None:
    """Flushing it early would defeat the cap it was suppressed by."""
    tenant_id, dest_id = scene
    await _insert(admin_session, tenant_id, dest_id, "suppressed", 5)
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()
    assert queued == []


async def test_a_stale_sending_row_is_requeued(scene, admin_session, monkeypatch) -> None:
    """A worker SIGKILLed between the claim and the send leaves the row in
    'sending' forever — no exception handler runs, so nothing else ever
    releases it. Nothing else looks at 'sending' rows: arq's retry requires
    status = 'pending' to reclaim. Only this sweep can recover it."""
    tenant_id, dest_id = scene
    row_id = await _insert(
        admin_session,
        tenant_id,
        dest_id,
        "sending",
        settings.NOTIFY_DELIVERY_STALE_MINUTES + 5,
    )
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append({"name": name, **kwargs})
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()

    assert any(q["delivery_id"] == str(row_id) for q in queued)


async def test_a_fresh_sending_row_is_left_alone(scene, admin_session, monkeypatch) -> None:
    """A row claimed moments ago by a worker that is merely slow, not dead,
    must not be swept out from under it — that would let a second worker
    claim it and double-message the recruiter."""
    tenant_id, dest_id = scene
    await _insert(admin_session, tenant_id, dest_id, "sending", 1)
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()
    assert queued == []


async def test_the_function_clamps_an_abusive_stale_window(
    scene, admin_session
) -> None:
    """`flush_notification_deliveries` is SECURITY DEFINER and bypasses RLS, so
    it must not trust `p_stale_minutes` just because today's only caller
    passes a fixed, sane value from settings. Call it directly with 0 — what
    a careless or malicious caller might pass — and confirm a 'sending' row
    claimed a second ago is still not promoted: the function clamps the
    window to a 1-minute floor internally rather than trusting the argument."""
    tenant_id, dest_id = scene
    row_id = await _insert(admin_session, tenant_id, dest_id, "sending", 0)

    result = await admin_session.execute(
        text("SELECT id FROM flush_notification_deliveries(0, 500)")
    )
    promoted = {row.id for row in result}
    assert row_id not in promoted


async def test_a_suppressed_burst_flushes_only_one_carrier(
    scene, admin_session, monkeypatch
) -> None:
    """Finding 2 of the final pre-merge review: the old function promoted
    EVERY hour-old suppressed row for a destination/event_kind at once, so a
    burst capped to a handful per hour was replayed message by message an hour
    later — on WhatsApp, each one billable. Only one row (the rollup carrier)
    should come back as `pending`; the rest must stay `suppressed` for
    deliver_notification's existing rollup read to fold into that carrier's
    "+N more" once it sends."""
    tenant_id, dest_id = scene
    ids = [
        await _insert(admin_session, tenant_id, dest_id, "suppressed", 90)
        for _ in range(5)
    ]
    queued: list[dict] = []

    async def fake_enqueue(name, **kwargs):
        queued.append(kwargs)
        return True

    monkeypatch.setattr(tasks, "enqueue", fake_enqueue)
    await tasks.flush_notifications()

    assert len(queued) == 1, "only the carrier should be requeued, not the batch"
    carrier_id = uuid.UUID(queued[0]["delivery_id"])
    assert carrier_id in ids

    async with tenant_session(tenant_id) as session:
        pending = (
            await session.execute(
                text(
                    "SELECT count(*) FROM notification_deliveries "
                    "WHERE id = ANY(:ids) AND status = 'pending'"
                ),
                {"ids": ids},
            )
        ).scalar_one()
        still_suppressed = (
            await session.execute(
                text(
                    "SELECT count(*) FROM notification_deliveries "
                    "WHERE id = ANY(:ids) AND status = 'suppressed'"
                ),
                {"ids": ids},
            )
        ).scalar_one()
    assert pending == 1
    assert still_suppressed == 4


def test_the_sweep_is_registered_in_the_supervisor() -> None:
    from app.workers.main import build_tasks

    assert "flush_notifications" in {t.name for t in build_tasks()}
