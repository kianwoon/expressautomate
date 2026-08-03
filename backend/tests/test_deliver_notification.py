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
from app.workers import delivery_jobs


class FakeChannel:
    """Records what it was asked to send and answers however the test says."""

    def __init__(self, result: SendResult) -> None:
        self.result = result
        self.sends: list[tuple[str, object]] = []

    async def send(self, address: str, content) -> SendResult:
        self.sends.append((address, content))
        return self.result


class ExplodingChannel:
    """Simulates a channel bug — something that is not a SendResult outcome
    at all, e.g. a KeyError from bad response parsing."""

    async def send(self, address: str, content) -> SendResult:
        raise KeyError("chat_id")


class ScriptedChannel:
    """Answers with a different, pre-scripted result on each successive call —
    e.g. a transient failure followed by a successful retry."""

    def __init__(self, results: list[SendResult]) -> None:
        self.results = list(results)
        self.sends: list[tuple[str, object]] = []

    async def send(self, address: str, content) -> SendResult:
        self.sends.append((address, content))
        return self.results[len(self.sends) - 1]


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
    # docstring in `app/workers/delivery_jobs.py`), so the row it points at must exist —
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
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    await delivery_jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )

    assert await _status(tenant_id, delivery_id) == STATUS_SENT
    assert len(fake.sends) == 1


async def test_the_channel_receives_the_decrypted_address(delivery, monkeypatch) -> None:
    """The column holds ciphertext; Telegram needs the chat id."""
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="1"))
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    await delivery_jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )
    assert fake.sends[0][0] == "12345"


async def test_a_second_run_does_not_send_again(delivery, monkeypatch) -> None:
    """The sweep and the original enqueue can both fire for one row."""
    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="1"))
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    for _ in range(2):
        await delivery_jobs.deliver_notification(
            {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )
    assert len(fake.sends) == 1


async def test_permanent_failure_disables_the_destination(delivery, monkeypatch) -> None:
    """A dead address must become visible, not absorb messages forever."""
    tenant_id, dest_id, delivery_id = delivery
    fake = FakeChannel(SendResult(outcome=SendOutcome.PERMANENT, error="bot blocked"))
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    await delivery_jobs.deliver_notification(
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
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    with pytest.raises(Exception):  # noqa: B017 — arq retries on any exception
        # Raising is how arq is told to retry — see the job's docstring.
        await delivery_jobs.deliver_notification(
            {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )

    assert await _status(tenant_id, delivery_id) == STATUS_PENDING


async def test_unexpected_exception_releases_the_row_back_to_pending(
    delivery, monkeypatch
) -> None:
    """A channel bug (KeyError, not a SendResult outcome) must not strand the
    row in 'sending' — nothing else ever looks at that status, so a row left
    there is lost silently and permanently."""
    tenant_id, _, delivery_id = delivery
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: ExplodingChannel())

    with pytest.raises(KeyError):
        await delivery_jobs.deliver_notification(
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
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    await delivery_jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )
    assert await _status(tenant_id, delivery_id) == STATUS_FAILED


async def test_a_missing_row_is_not_an_error(monkeypatch) -> None:
    """RLS already decided the job's tenant does not own this row."""
    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT))
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)
    await delivery_jobs.deliver_notification(
        {}, delivery_id=str(uuid.uuid4()), tenant_id=str(uuid.uuid4())
    )
    assert fake.sends == []


def test_the_job_is_registered_with_arq() -> None:
    """A name a producer enqueues but the registry omits fails inside arq, past
    the queue, where the producer already saw success."""
    from app.workers.settings import WorkerSettings

    assert delivery_jobs.deliver_notification in WorkerSettings.functions


async def test_an_opted_out_number_is_never_messaged(
    delivery, admin_session, monkeypatch
) -> None:
    """One agency's opt-out must stop every agency's sends — the reputation
    being spent belongs to a number they all share."""
    from app.core.crypto import encrypt
    from app.models.notification import CHANNEL_WHATSAPP, address_digest

    tenant_id, dest_id, delivery_id = delivery
    await admin_session.execute(
        text(
            "UPDATE notification_destinations "
            "SET channel = :ch, address_encrypted = :enc, address_hash = :hash "
            "WHERE id = :id"
        ),
        {
            "ch": CHANNEL_WHATSAPP,
            "enc": encrypt("+6591234567"),
            "hash": address_digest("+6591234567"),
            "id": dest_id,
        },
    )
    await admin_session.execute(
        text(
            "INSERT INTO whatsapp_suppressions (id, address_hash, reason) "
            "VALUES (gen_random_uuid(), :hash, 'user_stop')"
        ),
        {"hash": address_digest("+6591234567")},
    )
    await admin_session.commit()

    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT))
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    await delivery_jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )
    assert fake.sends == []
    assert await _status(tenant_id, delivery_id) == STATUS_FAILED

    await admin_session.execute(text("DELETE FROM whatsapp_suppressions"))
    await admin_session.commit()


async def _insert_suppressed(admin_session, tenant_id, dest_id, event_kind, n: int) -> None:
    from app.models.notification import STATUS_SUPPRESSED

    for _ in range(n):
        await admin_session.execute(
            text(
                "INSERT INTO notification_deliveries "
                "(id, tenant_id, destination_id, event_kind, subject_id, status) "
                "VALUES (:id, :tid, :did, :kind, :sub, :status)"
            ),
            {
                "id": uuid.uuid4(),
                "tid": tenant_id,
                "did": dest_id,
                "kind": event_kind,
                "sub": uuid.uuid4(),
                "status": STATUS_SUPPRESSED,
            },
        )
    await admin_session.commit()


async def test_a_transient_failure_leaves_the_rollup_batch_suppressed(
    delivery, admin_session, monkeypatch
) -> None:
    """Finding 1: the rollup must not be consumed before the send succeeds.
    A transient failure must leave the suppressed batch exactly as it was —
    not permanently marked 'rolled up' — so a retry (or the sweep) can still
    report it."""
    from app.models.notification import STATUS_SUPPRESSED
    from app.services.notify.events import EVENT_OPPORTUNITY_NEW

    tenant_id, dest_id, delivery_id = delivery
    await _insert_suppressed(admin_session, tenant_id, dest_id, EVENT_OPPORTUNITY_NEW, 2)

    fake = FakeChannel(SendResult(outcome=SendOutcome.TRANSIENT, error="503"))
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    with pytest.raises(Exception):  # noqa: B017 — arq retries on any exception
        await delivery_jobs.deliver_notification(
            {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )

    async with tenant_session(tenant_id) as session:
        remaining = (
            await session.execute(
                text(
                    "SELECT count(*) FROM notification_deliveries "
                    "WHERE destination_id = :d AND event_kind = :k AND status = :status"
                ),
                {"d": dest_id, "k": EVENT_OPPORTUNITY_NEW, "status": STATUS_SUPPRESSED},
            )
        ).scalar_one()
    assert remaining == 2


async def test_a_retry_after_transient_failure_still_reports_the_rollup(
    delivery, admin_session, monkeypatch
) -> None:
    """Finding 1, the full round trip: a transient failure followed by a
    successful retry must still carry the "+N more" — the count read before
    the failed attempt must not have been thrown away, and the batch must be
    retired only once the message that reports it actually sends."""
    from app.models.notification import STATUS_SUPPRESSED
    from app.services.notify.events import EVENT_OPPORTUNITY_NEW

    tenant_id, dest_id, delivery_id = delivery
    await _insert_suppressed(admin_session, tenant_id, dest_id, EVENT_OPPORTUNITY_NEW, 3)

    channel = ScriptedChannel(
        [
            SendResult(outcome=SendOutcome.TRANSIENT, error="503"),
            SendResult(outcome=SendOutcome.SENT, provider_message_id="1"),
        ]
    )
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: channel)

    with pytest.raises(Exception):  # noqa: B017
        await delivery_jobs.deliver_notification(
            {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )

    # The retry — arq would re-enqueue the same job; here we just call again.
    await delivery_jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )

    assert await _status(tenant_id, delivery_id) == STATUS_SENT
    assert len(channel.sends) == 2
    _, content = channel.sends[1]
    assert "3 more" in content.text

    async with tenant_session(tenant_id) as session:
        remaining = (
            await session.execute(
                text(
                    "SELECT count(*) FROM notification_deliveries "
                    "WHERE destination_id = :d AND event_kind = :k "
                    "AND status = :status"
                ),
                {"d": dest_id, "k": EVENT_OPPORTUNITY_NEW, "status": STATUS_SUPPRESSED},
            )
        ).scalar_one()
    assert remaining == 0


async def test_a_transient_failures_retry_after_reaches_arqs_defer(
    delivery, monkeypatch
) -> None:
    """Finding 3 of the final pre-merge review: both channel clients parse the
    provider's Retry-After into SendResult.retry_after, but the transient path
    used to raise a plain exception and throw it away, leaving arq to retry on
    its own schedule regardless of what the provider asked for. Honouring the
    hint is what keeps a throttled account from being throttled harder —
    exactly what GraphThrottled -> Retry(defer=...) already does on the
    ingestion side (app/workers/jobs.py, fetch_email)."""
    from arq import Retry

    tenant_id, _, delivery_id = delivery
    fake = FakeChannel(
        SendResult(outcome=SendOutcome.TRANSIENT, error="429", retry_after=17.5)
    )
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    with pytest.raises(Retry) as excinfo:
        await delivery_jobs.deliver_notification(
            {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
        )

    assert excinfo.value.defer_score == 17500


async def test_a_whatsapp_template_error_does_not_disable_the_destination(
    delivery, admin_session, monkeypatch
) -> None:
    """Finding 4: a template-config error (missing/paused template) is a
    deployment problem, not a dead address. The delivery still fails
    permanently — retrying can't help until a human fixes the template — but
    the destination itself must stay enabled."""
    from app.core.crypto import encrypt
    from app.models.notification import CHANNEL_WHATSAPP, address_digest

    tenant_id, dest_id, delivery_id = delivery
    await admin_session.execute(
        text(
            "UPDATE notification_destinations "
            "SET channel = :ch, address_encrypted = :enc, address_hash = :hash "
            "WHERE id = :id"
        ),
        {
            "ch": CHANNEL_WHATSAPP,
            "enc": encrypt("+6591234567"),
            "hash": address_digest("+6591234567"),
            "id": dest_id,
        },
    )
    await admin_session.commit()

    fake = FakeChannel(
        SendResult(
            outcome=SendOutcome.PERMANENT,
            error="template does not exist",
            disable_destination=False,
        )
    )
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name: fake)

    await delivery_jobs.deliver_notification(
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
    assert disabled is None


async def test_whatsapp_linked_send_carries_the_destinations_own_user(
    delivery, admin_session, monkeypatch
) -> None:
    """The only channel that sends on a person's own socket. The worker must
    pass the destination's own `user_id` through to `channel_for`, and the
    resulting channel must actually be used — the whole reason
    `channel_for` grew keyword arguments in the first place."""
    from app.core.crypto import encrypt
    from app.models.notification import CHANNEL_WHATSAPP_LINKED, address_digest

    tenant_id, dest_id, delivery_id = delivery
    await admin_session.execute(
        text(
            "UPDATE notification_destinations "
            "SET channel = :ch, address_encrypted = :enc, address_hash = :hash "
            "WHERE id = :id"
        ),
        {
            "ch": CHANNEL_WHATSAPP_LINKED,
            "enc": encrypt("+6591234567"),
            "hash": address_digest("+6591234567"),
            "id": dest_id,
        },
    )
    await admin_session.commit()

    fake = FakeChannel(SendResult(outcome=SendOutcome.SENT, provider_message_id="wamid.1"))
    seen_kwargs: dict = {}

    def _channel_for(name, **kwargs):
        seen_kwargs.update(kwargs)
        return fake

    monkeypatch.setattr(delivery_jobs, "channel_for", _channel_for)

    await delivery_jobs.deliver_notification(
        {}, delivery_id=str(delivery_id), tenant_id=str(tenant_id)
    )

    assert await _status(tenant_id, delivery_id) == STATUS_SENT
    assert len(fake.sends) == 1
    assert seen_kwargs["tenant_id"] == tenant_id
    assert seen_kwargs["user_id"] is not None


async def test_whatsapp_linked_permanent_with_disable_disables_the_destination(
    delivery, admin_session, monkeypatch
) -> None:
    """A logged-out linked device: PERMANENT with disable_destination=True
    (whatsapp_linked.py's dead-session branch) must disable exactly like the
    other channels' address-level PERMANENT failures do."""
    from app.core.crypto import encrypt
    from app.models.notification import CHANNEL_WHATSAPP_LINKED, address_digest

    tenant_id, dest_id, delivery_id = delivery
    await admin_session.execute(
        text(
            "UPDATE notification_destinations "
            "SET channel = :ch, address_encrypted = :enc, address_hash = :hash "
            "WHERE id = :id"
        ),
        {
            "ch": CHANNEL_WHATSAPP_LINKED,
            "enc": encrypt("+6591234567"),
            "hash": address_digest("+6591234567"),
            "id": dest_id,
        },
    )
    await admin_session.commit()

    fake = FakeChannel(
        SendResult(
            outcome=SendOutcome.PERMANENT,
            error="device logged out",
            disable_destination=True,
        )
    )
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name, **kwargs: fake)

    await delivery_jobs.deliver_notification(
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


async def test_whatsapp_linked_permanent_without_disable_leaves_it_enabled(
    delivery, admin_session, monkeypatch
) -> None:
    """The gateway-refused / outcome-unknown branches set
    disable_destination=False — the number itself is fine, so the destination
    must stay enabled just like whatsapp.py's template-config-error case does."""
    from app.core.crypto import encrypt
    from app.models.notification import CHANNEL_WHATSAPP_LINKED, address_digest

    tenant_id, dest_id, delivery_id = delivery
    await admin_session.execute(
        text(
            "UPDATE notification_destinations "
            "SET channel = :ch, address_encrypted = :enc, address_hash = :hash "
            "WHERE id = :id"
        ),
        {
            "ch": CHANNEL_WHATSAPP_LINKED,
            "enc": encrypt("+6591234567"),
            "hash": address_digest("+6591234567"),
            "id": dest_id,
        },
    )
    await admin_session.commit()

    fake = FakeChannel(
        SendResult(
            outcome=SendOutcome.PERMANENT,
            error="unknown outcome, not retried to avoid a duplicate",
            disable_destination=False,
        )
    )
    monkeypatch.setattr(delivery_jobs, "channel_for", lambda name, **kwargs: fake)

    await delivery_jobs.deliver_notification(
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
    assert disabled is None
