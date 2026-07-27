"""The ingestion pipeline emits, and knows nothing more than that.

`persist()` is the only place an opportunity is created, so it is also the
only place that can emit `opportunity.new` / `opportunity.needs_review`
inside the same transaction. These tests exercise that wiring end to end
(no Redis touched — the queue call is monkeypatched, same as
`test_extract_job.py` does for the worker jobs it wires) and separately lock
the two ordering properties the docstring in `dispatch.py` promises.

allow-hardcode: the SQL and fixture values below are test fixtures.
"""

import inspect
import uuid

import pytest
from sqlalchemy import text

from app.models.notification import CHANNEL_TELEGRAM, address_digest
from app.services.ingest import persist
from app.services.ingest.persist import persist as persist_fn
from app.services.ingest.schema import ExtractionResponse
from app.services.llm.client import LLMResult
from app.services.notify import dispatch as dispatch_module
from app.services.notify.events import EVENT_OPPORTUNITY_NEEDS_REVIEW, EVENT_OPPORTUNITY_NEW
from app.services.storage.r2 import body_key

SOURCE = "Finance officer at KLN Logistics. Salary up to $3500 per month."


def _payload(**overrides) -> dict:
    salary_at = SOURCE.index("up to $3500")
    period_at = SOURCE.index("per month")
    job = {
        "job_title": {
            "value": "Finance officer",
            "evidence": "Finance officer",
            "start_char": 0,
            "end_char": 15,
            "confidence": 0.95,
        },
        "salary": {
            "value": "3500",
            "evidence": "up to $3500",
            "start_char": salary_at,
            "end_char": salary_at + len("up to $3500"),
            "confidence": 0.9,
        },
        "salary_period": {
            "value": "month",
            "evidence": "per month",
            "start_char": period_at,
            "end_char": period_at + len("per month"),
            "confidence": 0.9,
        },
    }
    job.update(overrides)
    return {"jobs": [job]}


def _response(**overrides) -> ExtractionResponse:
    return ExtractionResponse.model_validate(_payload(**overrides))


@pytest.fixture
async def email_row(admin_session):
    tenant_id, mailbox_id, row_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:i, 'A', :s)"),
        {"i": tenant_id, "s": f"a-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes (id, tenant_id, ms_user_id, folder_id, scope,"
            " status, retention_months)"
            " VALUES (:i, :t, 'ms-user-1', 'inbox', 'folder', 'active', 24)"
        ),
        {"i": mailbox_id, "t": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id,"
            " received_datetime, processing_status, source_state, classification_status,"
            " body_html_r2_key, subject, sender_email)"
            " VALUES (:i, :t, :m, 'MSG-1', now(), 'classified', 'present',"
            " 'recruitment', :key, 'Finance officer', 'evelyn@example.com')"
        ),
        {
            "i": row_id,
            "t": tenant_id,
            "m": mailbox_id,
            "key": body_key(tenant_id, mailbox_id, "MSG-1", "html"),
        },
    )
    await admin_session.commit()
    yield tenant_id, mailbox_id, row_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id}
    )
    await admin_session.commit()


@pytest.fixture
async def subscribed(admin_session, email_row):
    """A verified, subscribed Telegram destination for `email_row`'s tenant,
    covering both event kinds this producer can emit."""
    tenant_id, _, _ = email_row
    user_id, dest_id = uuid.uuid4(), uuid.uuid4()
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
    for kind in (EVENT_OPPORTUNITY_NEW, EVENT_OPPORTUNITY_NEEDS_REVIEW):
        await admin_session.execute(
            text(
                "INSERT INTO notification_subscriptions "
                "(id, tenant_id, destination_id, event_kind, active) "
                "VALUES (:id, :tid, :did, :kind, true)"
            ),
            {"id": uuid.uuid4(), "tid": tenant_id, "did": dest_id, "kind": kind},
        )
    await admin_session.commit()
    return dest_id


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """`enqueue_deliveries` shells out to the queue after persist()'s
    transaction closes. Faked exactly like `test_notify_dispatch.py` fakes it,
    so these tests prove the outbox row exists without touching Redis."""

    async def _enqueue(job_name: str, **kwargs) -> bool:
        return True

    monkeypatch.setattr(dispatch_module, "enqueue", _enqueue)


async def _outbox_row(admin_session, tenant_id, opportunity_id):
    return (
        await admin_session.execute(
            text(
                "SELECT event_kind, subject_id FROM notification_deliveries "
                "WHERE tenant_id = :t AND subject_id = :s"
            ),
            {"t": tenant_id, "s": opportunity_id},
        )
    ).one_or_none()


async def test_persisting_a_clean_extraction_emits_opportunity_new(
    admin_session, email_row, subscribed
) -> None:
    tenant_id, _, eid = email_row
    ids = await persist_fn(
        tenant_id, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE
    )
    assert len(ids) == 1
    row = await _outbox_row(admin_session, tenant_id, ids[0])
    assert row is not None
    assert row.event_kind == EVENT_OPPORTUNITY_NEW


async def test_a_needs_review_extraction_emits_the_review_event(
    admin_session, email_row, subscribed
) -> None:
    tenant_id, _, eid = email_row
    # An evidence span that is nowhere in the source fails `verify()`, and
    # `quality_state` (app/services/ingest/evidence.py) demotes any extraction
    # with an unverifiable field to needs_review — the same rule
    # `_insert_opportunity` already relies on for `review_status`.
    thin = _response(
        job_title={
            "value": "Finance officer",
            "evidence": "this text does not appear in the source email",
            "start_char": 0,
            "end_char": 15,
            "confidence": 0.95,
        }
    )
    ids = await persist_fn(
        tenant_id, eid, thin, LLMResult(data={}, model="test/fast"), SOURCE
    )
    assert len(ids) == 1
    row = await _outbox_row(admin_session, tenant_id, ids[0])
    assert row is not None
    assert row.event_kind == EVENT_OPPORTUNITY_NEEDS_REVIEW


async def test_no_destination_means_no_outbox_row_and_no_error(
    admin_session, email_row
) -> None:
    """Nobody subscribed yet: `emit` legitimately writes nothing, and
    `persist` must still return the opportunity id without raising."""
    tenant_id, _, eid = email_row
    ids = await persist_fn(
        tenant_id, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE
    )
    assert len(ids) == 1
    assert await _outbox_row(admin_session, tenant_id, ids[0]) is None


def test_persist_does_not_know_about_channels() -> None:
    """If this fails, the abstraction has leaked into ingestion and adding a
    third channel means editing the extraction pipeline."""
    source = inspect.getsource(persist)
    assert "telegram" not in source.lower()
    assert "whatsapp" not in source.lower()


def test_notifications_are_enqueued_after_the_transaction_commits() -> None:
    """A job that starts before its row is visible reads nothing and exits.

    Locates the `async with tenant_session` block that wraps persist()'s
    writes and asserts the `emit(` call is inside it while the
    `enqueue_deliveries` call is outside — i.e. after the block has fully
    dedented back to the function's own indent level.
    """
    source = inspect.getsource(persist_fn)
    lines = source.splitlines()
    with_line = next(i for i, line in enumerate(lines) if "async with tenant_session" in line)
    with_indent = len(lines[with_line]) - len(lines[with_line].lstrip())

    emit_line = next(i for i, line in enumerate(lines) if "await emit(" in line)
    assert emit_line > with_line
    emit_indent = len(lines[emit_line]) - len(lines[emit_line].lstrip())
    assert emit_indent > with_indent, "emit() must run inside the tenant_session block"

    # First line after the `with` block whose indentation returns to (or
    # below) the `with` statement's own indent — that is where the block ends.
    block_end = next(
        i
        for i in range(with_line + 1, len(lines))
        if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= with_indent
    )

    enqueue_line = next(
        i for i, line in enumerate(lines) if "enqueue_deliveries" in line and "await" in line
    )
    assert enqueue_line >= block_end, "enqueue_deliveries must run after the transaction closes"
