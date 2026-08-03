"""Being told to wait is not a failed delivery attempt.

The WA gateway spaces sends tens of seconds apart on purpose, so an evening
batch of six notifications enqueues six jobs that mostly get refused. Before
`SendResult.backpressure` those refusals spent the row's attempt budget and the
tail of every burst landed `failed` — a real job order the recruiter never
heard about. These tests pin the two halves of the fix: a refusal costs
nothing, and it is bounded by wall clock instead.

The `delivery` fixture here is deliberately its own copy rather than an import
from `test_deliver_notification.py`: a fixture shared across files is a fixture
neither file can change, and this one has to be aged.
"""

import uuid

import pytest
import structlog
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
from app.services.notify.channels.base import (
    PermanentReason,
    SendOutcome,
    SendResult,
)
from app.workers import delivery_jobs

_SPACING_WAIT_SECONDS = 30.0


class ScriptedChannel:
    """Answers with the next pre-scripted result, repeating the last one."""

    def __init__(self, results: list[SendResult]) -> None:
        self.results = list(results)
        self.sends: list[tuple[str, object]] = []

    async def send(self, address: str, content) -> SendResult:
        self.sends.append((address, content))
        index = min(len(self.sends) - 1, len(self.results) - 1)
        return self.results[index]


class SpacedGateway:
    """The gateway's own rule: one send per session, then refuse for a while.

    Time is a counter the test advances rather than a clock, so a burst that
    takes three real minutes to drain takes none here.
    """

    def __init__(self, interval: float = _SPACING_WAIT_SECONDS) -> None:
        self.interval = interval
        self.now = 0.0
        self.last_sent_at: float | None = None
        self.sent = 0

    async def send(self, address: str, content) -> SendResult:
        if (
            self.last_sent_at is not None
            and self.now - self.last_sent_at < self.interval
        ):
            return SendResult(
                outcome=SendOutcome.TRANSIENT,
                error="too soon after the previous send",
                retry_after=self.interval - (self.now - self.last_sent_at),
                backpressure=True,
            )
        self.last_sent_at = self.now
        self.sent += 1
        return SendResult(
            outcome=SendOutcome.SENT, provider_message_id=f"m{self.sent}"
        )


class RecordingEnqueue:
    """Stands in for arq. Records the deferral instead of booking a job."""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls: list[dict] = []

    async def __call__(self, name: str, **kwargs) -> bool:
        self.calls.append({"name": name, **kwargs})
        return self.accepted


@pytest.fixture
async def burst(admin_session):
    """One destination, six pending deliveries — an evening batch.

    Yields `(tenant_id, destination_id, [delivery_id, ...])`. The single-row
    tests take the first id; the burst test drives all six.
    """
    from app.core.crypto import encrypt
    from app.services.notify.events import EVENT_OPPORTUNITY_NEW

    # allow-hardcode: SQL statements building fixture rows, not a phrase list.
    tenant_id, user_id, dest_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mailbox_id, email_message_id = uuid.uuid4(), uuid.uuid4()
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
    # The job re-reads the opportunity at send time, so the FK chain down to a
    # mailbox has to exist — see `deliver_notification`'s docstring.
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
            "INSERT INTO notification_destinations "
            "(id, tenant_id, user_id, channel, address_encrypted, address_hash, "
            "verified_at) "
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

    delivery_ids = []
    for index in range(6):
        opportunity_id, delivery_id = uuid.uuid4(), uuid.uuid4()
        await admin_session.execute(
            text(
                "INSERT INTO opportunities "
                "(id, tenant_id, email_message_id, job_title_raw, company_name_raw, "
                "location_raw, salary_raw, review_status, quality_state) "
                "VALUES (:id, :tid, :eid, :title, 'Acme', 'Singapore', 'SGD 6,000', "
                "'ready', 'likely')"
            ),
            {
                "id": opportunity_id,
                "tid": tenant_id,
                "eid": email_message_id,
                "title": f"Recruiter {index}",
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
        delivery_ids.append(delivery_id)
    await admin_session.commit()
    yield tenant_id, dest_id, delivery_ids
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


async def _delivery(tenant_id, delivery_id):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text(
                    "SELECT status, attempts, error FROM notification_deliveries "
                    "WHERE id = :id"
                ),
                {"id": delivery_id},
            )
        ).one()


async def _destination(tenant_id, dest_id):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text(
                    "SELECT failure_count, disabled_at FROM notification_destinations "
                    "WHERE id = :id"
                ),
                {"id": dest_id},
            )
        ).one()


async def _age(admin_session, delivery_id, minutes: int) -> None:
    """Backdate the row, since the deadline is measured from `created_at`."""
    await admin_session.execute(
        text(
            "UPDATE notification_deliveries "
            "SET created_at = now() - make_interval(mins => :mins) WHERE id = :id"
        ),
        {"id": delivery_id, "mins": minutes},
    )
    await admin_session.commit()


async def _run(tenant_id, delivery_id) -> None:
    await delivery_jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )


def _spaced(wait: float = _SPACING_WAIT_SECONDS) -> SendResult:
    return SendResult(
        outcome=SendOutcome.TRANSIENT,
        error="too soon after the previous send",
        retry_after=wait,
        backpressure=True,
    )


async def test_a_spacing_refusal_does_not_spend_an_attempt(
    burst, monkeypatch
) -> None:
    """The whole point: the gateway asking us to wait costs the row nothing."""
    tenant_id, _, ids = burst
    channel = ScriptedChannel([_spaced()])
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: channel)
    monkeypatch.setattr(delivery_jobs, "enqueue", RecordingEnqueue())

    # Returns normally — a deferral is not a failure, so arq must not retry it.
    await _run(tenant_id, ids[0])

    row = await _delivery(tenant_id, ids[0])
    assert row.status == STATUS_PENDING
    assert row.attempts == 0


async def test_a_spacing_refusal_books_a_job_for_the_providers_own_wait(
    burst, monkeypatch
) -> None:
    tenant_id, _, ids = burst
    channel = ScriptedChannel([_spaced(wait=17.5)])
    booked = RecordingEnqueue()
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: channel)
    monkeypatch.setattr(delivery_jobs, "enqueue", booked)

    await _run(tenant_id, ids[0])

    assert len(booked.calls) == 1
    assert booked.calls[0]["name"] == "deliver_notification"
    assert booked.calls[0]["delivery_id"] == str(ids[0])
    assert booked.calls[0]["_defer_by"] == 17.5


async def test_an_ordinary_transient_still_spends_an_attempt(
    burst, monkeypatch
) -> None:
    """A 503 is a real failed attempt and the budget must still shrink."""
    tenant_id, _, ids = burst
    channel = ScriptedChannel([SendResult(outcome=SendOutcome.TRANSIENT, error="503")])
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: channel)
    monkeypatch.setattr(delivery_jobs, "enqueue", RecordingEnqueue())

    with pytest.raises(Exception):  # noqa: B017 — arq retries on any exception
        await _run(tenant_id, ids[0])

    row = await _delivery(tenant_id, ids[0])
    assert row.status == STATUS_PENDING
    assert row.attempts == 1


async def test_an_ordinary_transient_is_never_deferred_by_this_path(
    burst, monkeypatch
) -> None:
    """Only backpressure books its own job; everything else is arq's retry."""
    tenant_id, _, ids = burst
    channel = ScriptedChannel([SendResult(outcome=SendOutcome.TRANSIENT, error="503")])
    booked = RecordingEnqueue()
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: channel)
    monkeypatch.setattr(delivery_jobs, "enqueue", booked)

    with pytest.raises(Exception):  # noqa: B017 — arq retries on any exception
        await _run(tenant_id, ids[0])

    assert booked.calls == []


async def test_endless_spacing_stops_at_the_deadline(
    burst, admin_session, monkeypatch
) -> None:
    """Not incrementing attempts would otherwise be an infinite loop."""
    tenant_id, _, ids = burst
    channel = ScriptedChannel([_spaced()])
    booked = RecordingEnqueue()
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: channel)
    monkeypatch.setattr(delivery_jobs, "enqueue", booked)
    await _age(
        admin_session, ids[0], settings.NOTIFY_BACKPRESSURE_DEADLINE_MINUTES + 1
    )

    with structlog.testing.capture_logs() as logs:
        await _run(tenant_id, ids[0])

    row = await _delivery(tenant_id, ids[0])
    assert row.status == STATUS_FAILED
    assert "backpressure" in row.error
    # And it stops for good: nothing further was booked.
    assert booked.calls == []
    expired = [
        entry
        for entry in logs
        if entry["event"] == "delivery_backpressure_deadline_exceeded"
    ]
    assert len(expired) == 1
    assert expired[0]["log_level"] == "error"


async def test_the_deadline_does_not_blame_the_recruiters_device(
    burst, admin_session, monkeypatch
) -> None:
    """A busy gateway says nothing about this address — no re-pairing.

    `_RECORD_FAILURE` on this path would count towards NOTIFY_MAX_FAILURES and
    eventually disable a destination that was never broken.
    """
    tenant_id, dest_id, ids = burst
    channel = ScriptedChannel([_spaced()])
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: channel)
    monkeypatch.setattr(delivery_jobs, "enqueue", RecordingEnqueue())
    await _age(
        admin_session, ids[0], settings.NOTIFY_BACKPRESSURE_DEADLINE_MINUTES + 1
    )

    await _run(tenant_id, ids[0])

    destination = await _destination(tenant_id, dest_id)
    assert destination.failure_count == 0
    assert destination.disabled_at is None


async def test_a_spaced_burst_still_delivers_its_tail(burst, monkeypatch) -> None:
    """Six at once, one send per interval — the sixth must arrive, not fail.

    This is the reported defect end to end. The re-queue is driven by hand
    because `enqueue` is faked: each pass advances the fake gateway's clock by
    one interval, exactly as a deferred arq job would.
    """
    tenant_id, dest_id, ids = burst
    gateway = SpacedGateway()
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: gateway)
    monkeypatch.setattr(delivery_jobs, "enqueue", RecordingEnqueue())

    outstanding = list(ids)
    # A generous cap that still fails loudly rather than hanging if the
    # re-queue ever stops making progress.
    for _ in range(len(ids) * 4):
        if not outstanding:
            break
        still_pending = []
        for delivery_id in outstanding:
            await _run(tenant_id, delivery_id)
            if (await _delivery(tenant_id, delivery_id)).status != STATUS_SENT:
                still_pending.append(delivery_id)
        outstanding = still_pending
        gateway.now += gateway.interval

    assert outstanding == []
    for delivery_id in ids:
        row = await _delivery(tenant_id, delivery_id)
        assert row.status == STATUS_SENT
        # Never charged for waiting: one attempt each, the one that sent.
        assert row.attempts == 1
    assert (await _destination(tenant_id, dest_id)).disabled_at is None


@pytest.mark.parametrize(
    ("reason", "event", "level"),
    [
        (PermanentReason.CONFIG, "delivery_permanently_failed_config", "error"),
        (PermanentReason.REJECTED, "delivery_rejected_by_provider", "warning"),
        (PermanentReason.UNKNOWN, "delivery_outcome_unknown", "error"),
    ],
)
async def test_the_three_address_is_fine_failures_log_distinctly(
    burst, monkeypatch, reason, event, level
) -> None:
    """An operator has to be able to tell these apart.

    All three leave the destination enabled, and all three used to log
    `delivery_permanently_failed_config` — which was a claim about a
    configuration problem that two of them cannot support (§15).
    """
    tenant_id, dest_id, ids = burst
    channel = ScriptedChannel(
        [
            SendResult(
                outcome=SendOutcome.PERMANENT,
                error="detail from the provider",
                disable_destination=False,
                permanent_reason=reason,
            )
        ]
    )
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: channel)
    monkeypatch.setattr(delivery_jobs, "enqueue", RecordingEnqueue())

    with structlog.testing.capture_logs() as logs:
        await _run(tenant_id, ids[0])

    matching = [entry for entry in logs if entry["event"] == event]
    assert len(matching) == 1
    assert matching[0]["log_level"] == level
    assert matching[0]["delivery_id"] == str(ids[0])
    # The address is fine in all three cases.
    assert (await _destination(tenant_id, dest_id)).disabled_at is None
