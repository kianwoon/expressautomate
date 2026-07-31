"""`opportunity.shared` / `opportunity.assigned` are emitted by production
code but had no entry in `_HEADLINE` or `_TEMPLATE_FOR`, so the delivery
worker raised `KeyError` on the row — and a crashing worker retries, stalling
every notification queued behind it. Same shape as the candidate fix.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.rls import tenant_session
from app.models.notification import (
    CHANNEL_TELEGRAM,
    CHANNEL_WHATSAPP,
    STATUS_SENT,
    address_digest,
)
from app.services.notify import events
from app.services.notify.channels.base import SendOutcome, SendResult
from app.services.notify.render import _HEADLINE, _TEMPLATE_FOR, render
from app.workers import jobs


def test_every_event_kind_can_be_rendered() -> None:
    """The guard that stops this recurring: any kind in `ALL_EVENT_KINDS`
    reaches the worker, and both dicts are indexed by kind unconditionally."""
    for kind in events.ALL_EVENT_KINDS:
        assert kind in _HEADLINE, f"{kind} missing from _HEADLINE"
        assert kind in _TEMPLATE_FOR, f"{kind} missing from _TEMPLATE_FOR"


@pytest.mark.parametrize(
    "kind", [events.EVENT_OPPORTUNITY_SHARED, events.EVENT_OPPORTUNITY_ASSIGNED]
)
def test_render_handles_the_share_kinds(kind: str) -> None:
    event = events.OpportunityEvent(
        kind=kind,
        tenant_id=uuid.uuid4(),
        opportunity_id=uuid.uuid4(),
        job_title="Backend Engineer",
        company_name="Acme Pte Ltd",
        location="Singapore",
        salary="8000",
        actor_name="Priya",
        note="urgent",
    )
    telegram = render(event, CHANNEL_TELEGRAM)
    assert "Backend Engineer" in telegram.text
    whatsapp = render(event, CHANNEL_WHATSAPP)
    assert str(event.opportunity_id) == whatsapp.button_param
    # A blank WhatsApp template parameter is rejected by Meta outright.
    assert all(param for param in whatsapp.body_params)


@pytest.fixture
async def share_delivery(admin_session, request):
    """A pending delivery of the requested kind to a verified Telegram
    destination — the point is that the worker can actually send one."""
    from app.core.crypto import encrypt

    kind = getattr(request, "param", events.EVENT_OPPORTUNITY_SHARED)
    # allow-hardcode: SQL statements building fixture rows, not a phrase list.
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    dest_id, delivery_id, opportunity_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
    # allow-hardcode: SQL statements building fixture rows, not a phrase list.
    # The delivery job re-reads the opportunity at send time, so the FK chain
    # down to a mailbox has to exist — same as `test_deliver_notification.py`.
    mailbox_id, email_message_id = uuid.uuid4(), uuid.uuid4()
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
            "VALUES (:id, :tid, :eid, 'Backend Engineer', 'Acme', 'Singapore', "
            "'SGD 8,000', 'ready', 'likely')"
        ),
        {"id": opportunity_id, "tid": tenant_id, "eid": email_message_id},
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
            "kind": kind,
            "sub": opportunity_id,
        },
    )
    await admin_session.commit()
    yield tenant_id, delivery_id, opportunity_id
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


@pytest.mark.parametrize(
    "share_delivery",
    [events.EVENT_OPPORTUNITY_SHARED, events.EVENT_OPPORTUNITY_ASSIGNED],
    indirect=True,
)
async def test_a_share_row_delivers_end_to_end(share_delivery, monkeypatch) -> None:
    """Emit-only tests pass on a system whose worker crashes on these rows."""
    tenant_id, delivery_id, _ = share_delivery
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
    assert "Backend Engineer" in fake.sends[0][1].text
