"""The delivery job: claim once, send once, and make a dead address visible."""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_TELEGRAM,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    address_digest,
)
from app.services.notify.channels.base import SendOutcome, SendResult
from app.workers import jobs


class FakeChannel:
    """Records what it was asked to send and answers however the test says."""

    def __init__(self, result: SendResult) -> None:
        self.result = result
        self.sends: list[tuple[str, object]] = []

    async def send(self, address: str, content) -> SendResult:
        self.sends.append((address, content))
        return self.result


@pytest.fixture
async def delivery(admin_session):
    """A pending delivery to a verified Telegram destination."""
    from app.core.crypto import encrypt
    from app.services.notify.events import EVENT_OPPORTUNITY_NEW

    # allow-hardcode: SQL statements building fixture rows, not a phrase list.
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    dest_id, delivery_id = uuid.uuid4(), uuid.uuid4()
    mailbox_id, email_message_id, opportunity_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
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
    # The delivery job re-reads the opportunity at send time (see the job's
    # docstring in `app/workers/jobs.py`), so the row it points at must exist —
    # which means walking the FK chain down to a mailbox, since that is what
    # `opportunities.email_message_id` and `email_messages.mailbox_id` require.
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes "
            "(id, tenant_id, ms_user_id, scope, folder_id, retention_months) "
            "VALUES (:id, :tid, 'ms-user', 'whole_inbox', 'inbox', 12)"
        ),
        {"id": mailbox_id, "tid": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id) "
            "VALUES (:id, :tid, :mid, 'graph-msg-1')"
        ),
        {"id": email_message_id, "tid": tenant_id, "mid": mailbox_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO opportunities "
            "(id, tenant_id, email_message_id, job_title_raw, company_name_raw, "
            "location_raw, salary_raw, review_status, quality_state) "
            "VALUES (:id, :tid, :eid, 'Recruiter', 'Acme', 'Singapore', 'SGD 6,000', "
            "'ready', 'likely')"
        ),
        {"id": opportunity_id, "tid": tenant_id, "eid": email_message_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, verified_at) "
            "VALUES (:id, :tid, :uid, :ch, :enc, :hash, now())"
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
            "kind": EVENT_OPPORTUNITY_NEW,
            "sub": opportunity_id,
        },
    )
    await admin_session.commit()
    yield tenant_id, dest_id, delivery_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def _status(tenant_id, delivery_id) -> str:
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text("SELECT status FROM notification_deliveries WHERE id = :id"),
                {"id": delivery_id},
            )
        ).scalar_one()


async def test_successful_send_marks_the_row_sent(delivery, monkeypatch) -> None:
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="42"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )

    assert await _status(tenant_id, delivery_id) == STATUS_SENT
    assert len(fake.sends) == 1


async def test_the_channel_receives_the_decrypted_address(delivery, monkeypatch) -> None:
    """The column holds ciphertext; Telegram needs the chat id."""
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="1"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )
    assert fake.sends[0][0] == "12345"


async def test_a_second_run_does_not_send_again(delivery, monkeypatch) -> None:
    """The sweep and the original enqueue can both fire for one row."""
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="1"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    for _ in range(2):
        await jobs.deliver_notification(
            {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )
    assert len(fake.sends) == 1


async def test_permanent_failure_disables_the_destination(delivery, monkeypatch) -> None:
    """A dead address must become visible, not absorb messages forever."""
    tenant_id, dest_id, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.PERMANENT, error="bot blocked"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )

    assert await _status(tenant_id, delivery_id) == STATUS_FAILED
    async with tenant_session(tenant_id) as session:
        disabled = (
            await session.execute(
                text("SELECT disabled_at FROM notification_destinations WHERE id = :id"),
                {"id": dest_id},
            )
        ).scalar_one()
    assert disabled is not None


async def test_transient_failure_returns_the_row_to_pending(delivery, monkeypatch) -> None:
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.TRANSIENT, error="503"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    with pytest.raises(Exception):  # noqa: B017 — arq retries on any exception
        # Raising is how arq is told to retry — see the job's docstring.
        await jobs.deliver_notification(
            {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )

    assert await _status(tenant_id, delivery_id) == STATUS_PENDING


async def test_transient_failure_past_max_attempts_gives_up(
    delivery, monkeypatch, admin_session
) -> None:
    """A message about a vacancy from six hours ago is not worth a seventh try."""
    tenant_id, _, delivery_id = delivery
    await admin_session.execute(
        text("UPDATE notification_deliveries SET attempts = :n WHERE id = :id"),
        {"n": settings.NOTIFY_MAX_ATTEMPTS, "id": delivery_id},
    )
    await admin_session.commit()

    fake = FakeChannel(SendResult(outcome=SendOutcome.TRANSIENT, error="503"))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)

    await jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )
    assert await _status(tenant_id, delivery_id) == STATUS_FAILED


async def test_a_missing_row_is_not_an_error(monkeypatch) -> None:
    """RLS already decided the job's tenant does not own this row."""
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT))
    monkeypatch.setattr(jobs, "channel_for", lambda name: fake)
    await jobs.deliver_notification(
        {}, delivery_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4())
    )
    assert fake.sends == []


def test_the_job_is_registered_with_arq() -> None:
    """A name a producer enqueues but the registry omits fails inside arq, past
    the queue, where the producer already saw success."""
    from app.workers.settings import WorkerSettings

    assert jobs.deliver_notification in WorkerSettings.functions
