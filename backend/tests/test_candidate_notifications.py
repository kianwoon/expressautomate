"""A tenant broadcast is one event with N recipients, not N events."""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_TELEGRAM,
    CHANNEL_WHATSAPP,
    STATUS_SENT,
    address_digest,
)
from app.services.notify import events
from app.services.notify.channels.base import SendOutcome, SendResult
from app.workers import jobs


def test_the_six_candidate_kinds_exist() -> None:
    assert events.CANDIDATE_SHARED == "candidate.shared"
    assert events.CANDIDATE_ASSIGNED == "candidate.assigned"
    assert events.CANDIDATE_UNCLAIMED == "candidate.unclaimed"
    assert events.CANDIDATE_ACCESS_REQUESTED == "candidate.access_requested"
    assert events.CANDIDATE_ACCESS_GRANTED == "candidate.access_granted"
    assert events.CANDIDATE_ACCESS_DECLINED == "candidate.access_declined"


def test_every_kind_fits_the_column() -> None:
    """`event_kind` is String(48). A kind that does not fit fails at insert,
    in production, on the first share."""
    for name in dir(events):
        if name.startswith("CANDIDATE_"):
            value = getattr(events, name)
            if isinstance(value, str):
                assert len(value) <= 48


def test_every_candidate_kind_is_subscribable() -> None:
    """Not cosmetic: a kind missing from `ALL_EVENT_KINDS` can have no
    subscribers, so every event of it delivers to nobody — which looks
    exactly like the feature working quietly."""
    for kind in events.CANDIDATE_EVENT_KINDS:
        assert kind in events.ALL_EVENT_KINDS


def test_a_candidate_event_carries_recipients() -> None:
    from app.services.notify.candidate_events import CandidateEvent

    event = CandidateEvent(
        kind=events.CANDIDATE_SHARED,
        tenant_id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        candidate_name="Wei Ming Tan",
        recipient_user_ids=(uuid.uuid4(),),
    )
    # `None` keeps the tenant-wide meaning, exactly as OpportunityEvent's does.
    assert event.recipient_user_ids is not None
    assert event.subject_id == event.candidate_id
    assert (
        CandidateEvent(
            kind=events.CANDIDATE_UNCLAIMED,
            tenant_id=uuid.uuid4(),
            candidate_id=uuid.uuid4(),
            candidate_name="Wei Ming Tan",
        ).recipient_user_ids
        is None
    )


def test_an_opportunity_event_exposes_the_same_subject_id() -> None:
    """The two types meet at exactly this protocol and nowhere else."""
    opportunity_id = uuid.uuid4()
    event = events.OpportunityEvent(
        kind=events.EVENT_OPPORTUNITY_NEW,
        tenant_id=uuid.uuid4(),
        opportunity_id=opportunity_id,
        job_title=None,
        company_name=None,
        location=None,
        salary=None,
    )
    assert event.subject_id == opportunity_id


def test_render_handles_every_candidate_kind() -> None:
    """`_HEADLINE` / `_TEMPLATE_FOR` are plain dicts — a missing kind is a
    KeyError in the worker, which retries forever and stalls the queue."""
    from app.services.notify.candidate_events import CandidateEvent
    from app.services.notify.render import render

    for kind in events.CANDIDATE_EVENT_KINDS:
        event = CandidateEvent(
            kind=kind,
            tenant_id=uuid.uuid4(),
            candidate_id=uuid.uuid4(),
            candidate_name="Wei Ming Tan",
            actor_name="Priya",
            note="urgent",
        )
        telegram = render(event, CHANNEL_TELEGRAM)
        assert "Wei Ming Tan" in telegram.text
        whatsapp = render(event, CHANNEL_WHATSAPP)
        assert str(event.candidate_id) == whatsapp.button_param
        assert "Wei Ming Tan" in whatsapp.body_params


def test_render_tolerates_a_nameless_candidate() -> None:
    from app.services.notify.candidate_events import CandidateEvent
    from app.services.notify.render import render

    event = CandidateEvent(
        kind=events.CANDIDATE_SHARED,
        tenant_id=uuid.uuid4(),
        candidate_id=uuid.uuid4(),
        candidate_name=None,
    )
    # A blank WhatsApp template parameter is rejected by Meta outright.
    assert all(param for param in render(event, CHANNEL_WHATSAPP).body_params)


@pytest.fixture
async def candidate_delivery(admin_session):
    """A pending `candidate.shared` delivery to a verified Telegram
    destination — the whole point of this task is that the worker can
    actually send one."""
    from app.core.crypto import encrypt

    # allow-hardcode: SQL statements building fixture rows, not a phrase list.
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    dest_id, delivery_id, candidate_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
            "INSERT INTO candidates (id, tenant_id, full_name) "
            "VALUES (:id, :tid, 'Wei Ming Tan')"
        ),
        {"id": candidate_id, "tid": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, "
            "verified_at) VALUES (:id, :tid, :uid, :ch, :enc, :hash, now())"
        ),
        {
            "id": dest_id,
            "tid": tenant_id,
            "uid": user_id,
            "ch": CHANNEL_TELEGRAM,
            "enc": encrypt("12345"),
            "hash": address_digest("12345"),
        },
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_deliveries "
            "(id, tenant_id, destination_id, event_kind, subject_id, status) "
            "VALUES (:id, :tid, :did, :kind, :sub, 'pending')"
        ),
        {
            "id": delivery_id,
            "tid": tenant_id,
            "did": dest_id,
            "kind": events.CANDIDATE_SHARED,
            "sub": candidate_id,
        },
    )
    await admin_session.commit()
    yield tenant_id, delivery_id, candidate_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


class FakeChannel:
    def __init__(self, result: SendResult) -> None:
        self.result = result
        self.sends: list[tuple[str, object]] = []

    async def send(self, address: str, content) -> SendResult:
        self.sends.append((address, content))
        return self.result


async def test_a_candidate_row_delivers_end_to_end(
    candidate_delivery, monkeypatch
) -> None:
    """Emit-only tests pass on a system whose worker crashes on these rows."""
    tenant_id, delivery_id, _ = candidate_delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="7"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )

    async with tenant_session(tenant_id) as session:
        status = (
            await session.execute(
                text("SELECT status FROM notification_deliveries WHERE id = :id"),
                {"id": delivery_id},
            )
        ).scalar_one()
    assert status == STATUS_SENT
    assert len(fake.sends) == 1
    assert "Wei Ming Tan" in fake.sends[0][1].text


async def test_a_deleted_candidate_does_not_crash_the_worker(
    candidate_delivery, admin_session, monkeypatch
) -> None:
    tenant_id, delivery_id, candidate_id = candidate_delivery
    await admin_session.execute(
        text("DELETE FROM candidates WHERE id = :id"), {"id": candidate_id}
    )
    await admin_session.commit()
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="7"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )
    assert fake.sends == []


async def test_emit_candidate_event_writes_one_row_per_subscriber(
    candidate_delivery, admin_session, monkeypatch
) -> None:
    """A tenant broadcast is ONE event with N recipients, not N events."""
    from app.services.notify.candidate_events import CandidateEvent
    from app.services.notify.dispatch import emit_candidate_event

    tenant_id, _, candidate_id = candidate_delivery
    other_candidate = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO candidates (id, tenant_id, full_name) "
            "VALUES (:id, :tid, 'Siti Rahim')"
        ),
        {"id": other_candidate, "tid": tenant_id},
    )
    dest_id = (
        await admin_session.execute(
            text(
                "SELECT id FROM notification_destinations WHERE tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
    ).scalar_one()
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
            "kind": events.CANDIDATE_SHARED,
        },
    )
    await admin_session.commit()

    monkeypatch.setattr(
        "app.services.notify.dispatch.enqueue", _never_enqueue, raising=True
    )
    queued = await emit_candidate_event(
        CandidateEvent(
            kind=events.CANDIDATE_SHARED,
            tenant_id=tenant_id,
            candidate_id=other_candidate,
            candidate_name="Siti Rahim",
        )
    )
    assert queued == 1

    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM notification_deliveries "
                    "WHERE subject_id = :sid AND event_kind = :kind"
                ),
                {"sid": other_candidate, "kind": events.CANDIDATE_SHARED},
            )
        ).scalar_one()
    assert rows == 1


async def _never_enqueue(*args, **kwargs) -> bool:
    """The queue is not what this test is about; Redis may not be running."""
    return True


def test_the_rate_cap_setting_is_still_what_bounds_a_broadcast() -> None:
    assert settings.NOTIFY_RATE_CAP_PER_HOUR > 0
