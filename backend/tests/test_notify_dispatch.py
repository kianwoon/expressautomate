"""Dispatch: who is subscribed, what lands in the outbox, and what the cap eats."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_TELEGRAM,
    STATUS_PENDING,
    STATUS_SUPPRESSED,
    address_digest,
)
from app.services.notify import dispatch
from app.services.notify.dispatch import emit, enqueue_deliveries, rate_capped
from app.services.notify.events import EVENT_OPPORTUNITY_NEW, OpportunityEvent


@pytest.fixture
async def wired(admin_session):
    """One tenant, one user, one verified Telegram destination subscribed to
    EVENT_OPPORTUNITY_NEW."""
    tenant_id, user_id, dest_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # `slug` is NOT NULL + unique on `tenants` (app/models/tenant.py) — the
    # brief's fixture omitted it; every other test file in this repo supplies
    # one, so this fixture follows the same convention.
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'agency', :slug)"),
        {"id": tenant_id, "slug": f"agency-{tenant_id}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, role) "
            "VALUES (:id, :tid, 'r@agency.sg', 'recruiter')"
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
    await admin_session.execute(
        text(
            "INSERT INTO notification_subscriptions "
            "(id, tenant_id, destination_id, event_kind, active) "
            "VALUES (:id, :tid, :did, :kind, true)"
        ),
        {
            "id": uuid.uuid4(),
            "tid": tenant_id,
            "did": dest_id,
            "kind": EVENT_OPPORTUNITY_NEW,
        },
    )
    await admin_session.commit()
    yield tenant_id, user_id, dest_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def _event(tenant_id: uuid.UUID) -> OpportunityEvent:
    return OpportunityEvent(
        kind=EVENT_OPPORTUNITY_NEW,
        tenant_id=tenant_id,
        opportunity_id=uuid.uuid4(),
        job_title="Engineer",
        company_name="Acme",
        location="Singapore",
        salary="SGD 8,000",
    )


async def test_emit_writes_one_pending_row_per_subscriber(wired) -> None:
    tenant_id, _, dest_id = wired
    async with tenant_session(tenant_id) as session:
        ids = await emit(_event(tenant_id), session)
    assert len(ids) == 1

    async with tenant_session(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT destination_id, status FROM notification_deliveries "
                    "WHERE id = :id"
                ),
                {"id": ids[0]},
            )
        ).one()
    assert row.destination_id == dest_id
    assert row.status == STATUS_PENDING


async def test_emit_ignores_an_inactive_subscription(wired, admin_session) -> None:
    tenant_id, _, dest_id = wired
    await admin_session.execute(
        text("UPDATE notification_subscriptions SET active = false WHERE destination_id = :d"),
        {"d": dest_id},
    )
    await admin_session.commit()
    async with tenant_session(tenant_id) as session:
        assert await emit(_event(tenant_id), session) == []


async def test_emit_ignores_an_unverified_destination(wired, admin_session) -> None:
    """An unverified address is one somebody typed. It may not be theirs."""
    tenant_id, _, dest_id = wired
    await admin_session.execute(
        text("UPDATE notification_destinations SET verified_at = NULL WHERE id = :d"),
        {"d": dest_id},
    )
    await admin_session.commit()
    async with tenant_session(tenant_id) as session:
        assert await emit(_event(tenant_id), session) == []


async def test_emit_ignores_a_disabled_destination(wired, admin_session) -> None:
    tenant_id, _, dest_id = wired
    await admin_session.execute(
        text("UPDATE notification_destinations SET disabled_at = now() WHERE id = :d"),
        {"d": dest_id},
    )
    await admin_session.commit()
    async with tenant_session(tenant_id) as session:
        assert await emit(_event(tenant_id), session) == []


async def test_emit_is_idempotent_for_one_opportunity(wired) -> None:
    """The extraction job can be retried. The recruiter must not be told twice."""
    tenant_id, _, _ = wired
    event = _event(tenant_id)
    async with tenant_session(tenant_id) as session:
        first = await emit(event, session)
    async with tenant_session(tenant_id) as session:
        second = await emit(event, session)
    assert len(first) == 1
    assert second == []


async def test_emit_does_not_reach_another_tenant(wired, admin_session) -> None:
    """A destination in tenant A must never receive tenant B's job orders."""
    tenant_id, _, _ = wired
    other = uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'other', :slug)"),
        {"id": other, "slug": f"other-{other}"},
    )
    await admin_session.commit()
    try:
        async with tenant_session(other) as session:
            assert await emit(_event(other), session) == []
    finally:
        await admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": other}
        )
        await admin_session.commit()


async def test_rate_cap_suppresses_past_the_hourly_ceiling(wired, admin_session) -> None:
    tenant_id, _, dest_id = wired
    for _ in range(settings.NOTIFY_RATE_CAP_PER_HOUR):
        await admin_session.execute(
            text(
                "INSERT INTO notification_deliveries "
                "(id, tenant_id, destination_id, event_kind, subject_id, status) "
                "VALUES (:id, :tid, :did, :kind, :sub, 'sent')"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "did": dest_id,
                "kind": EVENT_OPPORTUNITY_NEW,
                "sub": uuid.uuid4(),
            },
        )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        assert await rate_capped(session, dest_id, EVENT_OPPORTUNITY_NEW) is True
        ids = await emit(_event(tenant_id), session)

    async with tenant_session(tenant_id) as session:
        status = (
            await session.execute(
                text("SELECT status FROM notification_deliveries WHERE id = :id"),
                {"id": ids[0]},
            )
        ).scalar_one()
    assert status == STATUS_SUPPRESSED


async def test_enqueue_deliveries_skips_a_suppressed_row_even_if_handed_its_id(
    wired, admin_session, monkeypatch
) -> None:
    """The trap this closes: a caller that emits and enqueues as two separate
    steps (rather than through `emit_and_enqueue`) must not be able to queue a
    rate-capped row just because `emit()` handed back its id. `emit()`
    legitimately returns every id it wrote, suppressed included — so the
    guard has to live in `enqueue_deliveries`, checked against the row's own
    status, not trusted from the caller's list.
    """
    tenant_id, _, dest_id = wired
    for _ in range(settings.NOTIFY_RATE_CAP_PER_HOUR):
        await admin_session.execute(
            text(
                "INSERT INTO notification_deliveries "
                "(id, tenant_id, destination_id, event_kind, subject_id, status) "
                "VALUES (:id, :tid, :did, :kind, :sub, 'sent')"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "did": dest_id,
                "kind": EVENT_OPPORTUNITY_NEW,
                "sub": uuid.uuid4(),
            },
        )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        ids = await emit(_event(tenant_id), session)
    assert len(ids) == 1  # written, but suppressed by the cap above

    queued_ids: list[str] = []

    async def _enqueue(job_name: str, **kwargs) -> bool:
        queued_ids.append(kwargs["delivery_id"])
        return True

    monkeypatch.setattr(dispatch, "enqueue", _enqueue)

    queued = await enqueue_deliveries(tenant_id, ids)

    assert queued == 0
    assert queued_ids == []


async def test_rate_cap_ignores_sends_older_than_an_hour(wired, admin_session) -> None:
    """The window slides. Yesterday's burst must not mute today."""
    tenant_id, _, dest_id = wired
    stale = datetime.now(UTC) - timedelta(hours=2)
    for _ in range(settings.NOTIFY_RATE_CAP_PER_HOUR + 5):
        await admin_session.execute(
            text(
                "INSERT INTO notification_deliveries "
                "(id, tenant_id, destination_id, event_kind, subject_id, status, created_at) "
                "VALUES (:id, :tid, :did, :kind, :sub, 'sent', :ts)"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "did": dest_id,
                "kind": EVENT_OPPORTUNITY_NEW,
                "sub": uuid.uuid4(),
                "ts": stale,
            },
        )
    await admin_session.commit()

    async with tenant_session(tenant_id) as session:
        assert await rate_capped(session, dest_id, EVENT_OPPORTUNITY_NEW) is False


async def test_enqueue_deliveries_returns_zero_if_pending_status_check_fails(
    wired, monkeypatch
) -> None:
    """Guard the query: a transient database error must not propagate, only log.

    The pending-status query is called after the row is already committed, so it
    is the step allowed to fail without harming the producer. A lost enqueue is
    recovered by `rescan_stuck`, but an exception here fails the extraction job
    and makes it retry work that already succeeded — the same recovery path,
    unneeded.
    """
    tenant_id, _, _ = wired
    delivery_id = uuid.uuid4()

    # Make tenant_session return a session that raises on execute.
    class FailingAsyncContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, *args, **kwargs):
            raise RuntimeError("database connection lost")

    def failing_session(*args, **kwargs):
        return FailingAsyncContext()

    monkeypatch.setattr("app.services.notify.dispatch.tenant_session", failing_session)

    result = await enqueue_deliveries(tenant_id, [delivery_id])
    assert result == 0
