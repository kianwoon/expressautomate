"""The fetch_email job (plan §7, §10).

Where Graph, R2 and Postgres meet, and where the ordering constraints live.

The one that matters most: the body lands in R2 **before** the row's status
flips. A crash between those two must cost a repeated write on retry, never a
row pointing at an object that was never stored — the extraction job would then
read nothing and record confident emptiness.

The job takes its tenant in the payload rather than looking it up through an
RLS-bypassing function. RLS validates the pair for free: a job naming a
mismatched (tenant, row) reads nothing and no-ops.

allow-hardcode: the SQL and the Graph payload below are test fixtures.
"""

import uuid

import httpx
import pytest
from arq import Retry
from sqlalchemy import text

from app.db.rls import tenant_session
from app.services.graph.client import GraphClient
from app.services.storage.r2 import InMemoryBodyStore, body_key
from app.workers import jobs

GRAPH_MESSAGE = {
    "id": "MSG-1",
    "internetMessageId": "<abc@example.com>",
    "conversationId": "CONV-1",
    "subject": "Finance officer — KLN Logistics",
    "receivedDateTime": "2026-07-27T02:15:00Z",
    "hasAttachments": False,
    "from": {"emailAddress": {"name": "Evelyn Xie", "address": "evelynxie@example.com"}},
    "body": {"contentType": "html", "content": "<p>Up to $3500</p>"},
    "bodyPreview": "Up to $3500",
}


@pytest.fixture
async def pending(admin_session):
    """A tenant, mailbox and one pending email row."""
    tenant_id, mailbox_id, row_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"a-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user-1', 'inbox', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    await admin_session.execute(
        text(
            "INSERT INTO email_messages (id, tenant_id, mailbox_id, graph_message_id)"
            " VALUES (:id, :tenant, :mailbox, 'MSG-1')"
        ),
        {"id": row_id, "tenant": tenant_id, "mailbox": mailbox_id},
    )
    await admin_session.commit()
    yield tenant_id, mailbox_id, row_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def _wire(monkeypatch, handler, store=None):
    """Fake Graph, fake R2, captured queue. Nothing reaches the network."""
    store = store or InMemoryBodyStore()
    queued: list[tuple[str, dict]] = []

    async def _client(tenant_id, mailbox_id):
        return GraphClient(token="t", transport=httpx.MockTransport(handler))

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    monkeypatch.setattr(jobs, "graph_client_for_mailbox", _client)
    monkeypatch.setattr(jobs, "body_store", lambda: store)
    monkeypatch.setattr(jobs, "enqueue", _enqueue)
    return store, queued


def _ok(request):
    return httpx.Response(200, json=GRAPH_MESSAGE)


async def _row(tenant_id, row_id):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text("SELECT * FROM email_messages WHERE id = :id"), {"id": row_id}
            )
        ).one()


async def _run(pending_fixture):
    tenant_id, mailbox_id, row_id = pending_fixture
    await jobs.fetch_email(
        {},
        email_message_id=str(row_id),
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )


# --- the happy path ---------------------------------------------------------


async def test_the_message_and_its_body_are_stored(monkeypatch, pending):
    tenant_id, mailbox_id, row_id = pending
    store, queued = _wire(monkeypatch, _ok)

    await _run(pending)

    row = await _row(tenant_id, row_id)
    assert row.processing_status == "fetched"
    assert row.sender_email == "evelynxie@example.com"
    assert row.sender_name == "Evelyn Xie"
    assert row.subject == "Finance officer — KLN Logistics"
    assert row.internet_message_id == "<abc@example.com>"
    assert row.conversation_id == "CONV-1"
    assert row.has_attachments is False

    assert store.objects[body_key(tenant_id, mailbox_id, "MSG-1", "html")] == (
        "<p>Up to $3500</p>"
    )
    assert queued == [("classify_email", {
        "email_message_id": str(row_id),
        "tenant_id": str(tenant_id),
        "mailbox_id": str(mailbox_id),
    })]


async def test_the_received_date_is_recorded(monkeypatch, pending):
    """The column the target spreadsheet was missing — and the one every
    analytic in §25 sorts and buckets by."""
    tenant_id, _, row_id = pending
    _wire(monkeypatch, _ok)

    await _run(pending)

    row = await _row(tenant_id, row_id)
    assert row.received_datetime is not None
    assert row.received_datetime.year == 2026
    assert row.received_datetime.tzinfo is not None, "must stay timezone-aware"


async def test_retention_is_stamped_at_write_time(monkeypatch, pending):
    """Stamping now means purging never has to recompute policy over history,
    and a later change to a tenant's retention does not retroactively delete."""
    tenant_id, _, row_id = pending
    _wire(monkeypatch, _ok)

    await _run(pending)

    row = await _row(tenant_id, row_id)
    assert row.retention_until is not None


async def test_the_attempt_is_counted(monkeypatch, pending):
    tenant_id, _, row_id = pending
    _wire(monkeypatch, _ok)

    await _run(pending)

    assert (await _row(tenant_id, row_id)).attempt_count == 1


async def test_the_request_targets_the_mailbox_and_message(monkeypatch, pending):
    """The mailbox is addressed by its Graph user id, not the tenant's."""
    seen = {}

    def _capture(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json=GRAPH_MESSAGE)

    _wire(monkeypatch, _capture)

    await _run(pending)

    assert seen["path"].endswith("/users/ms-user-1/messages/MSG-1")


def test_a_message_id_containing_a_slash_stays_one_path_segment():
    """Graph ids are base64-derived and can contain `/` and `+`.

    Interpolated raw, a `/` splits into extra path segments and the request
    404s on a message that exists — which the fetch job would then record as
    `unfetchable`, permanently, for a message sitting in the mailbox.
    """
    path = jobs._message_path("user@example.com", "AAkAL/g+w==")

    assert path.count("/") == 4, "users/<id>/messages/<id> and nothing more"
    assert "%2F" in path
    assert path.endswith("AAkAL%2Fg%2Bw%3D%3D")


# --- ordering ---------------------------------------------------------------


async def test_the_body_lands_before_the_status_moves(monkeypatch, pending):
    """A crash between the two must leave the row retryable.

    The reverse order would leave a `fetched` row pointing at an object that
    was never written, and extraction would read nothing and record confident
    emptiness — a wrong answer rather than a visible failure.
    """
    tenant_id, _, row_id = pending

    class _Exploding(InMemoryBodyStore):
        async def put(self, key, content):
            raise RuntimeError("R2 unavailable")

    _wire(monkeypatch, _ok, store=_Exploding())

    with pytest.raises(RuntimeError):
        await _run(pending)

    row = await _row(tenant_id, row_id)
    assert row.processing_status == "pending", "still retryable"
    assert row.body_html_r2_key is None, "no key for an object that was not written"


# --- failure modes ----------------------------------------------------------


async def test_a_deleted_message_is_terminal(monkeypatch, pending):
    """The source is genuinely gone. Recording that is honest; retrying is not."""
    tenant_id, _, row_id = pending
    _, queued = _wire(monkeypatch, lambda r: httpx.Response(404, json={}))

    await _run(pending)

    row = await _row(tenant_id, row_id)
    assert row.processing_status == "unfetchable"
    assert row.source_state == "deleted"
    assert queued == [], "nothing downstream to do"


async def test_throttling_defers_the_job_for_the_delay_graph_asked_for(
    monkeypatch, pending
):
    """arq only reschedules on `Retry`. A bare exception is a failed job, and
    Graph's own Retry-After would be thrown away."""
    tenant_id, _, row_id = pending
    _wire(
        monkeypatch,
        lambda r: httpx.Response(429, headers={"Retry-After": "5"}, json={}),
    )

    with pytest.raises(Retry) as excinfo:
        await _run(pending)

    assert excinfo.value.defer_score == 5000  # arq stores the defer in ms
    assert (await _row(tenant_id, row_id)).processing_status == "pending"


async def test_a_revoked_grant_marks_the_mailbox_for_reconnection(monkeypatch, pending):
    """403 answers the same way forever. Retrying would bury the cause under
    exhausted attempts; the honest response is to stop and tell the user."""
    tenant_id, mailbox_id, row_id = pending
    _wire(monkeypatch, lambda r: httpx.Response(403, json={}))

    await _run(pending)

    async with tenant_session(tenant_id) as session:
        status = (
            await session.execute(
                text("SELECT status FROM mailboxes WHERE id = :id"), {"id": mailbox_id}
            )
        ).scalar_one()
    assert status == "needs_reauth"
    assert (await _row(tenant_id, row_id)).processing_status == "pending"


# --- idempotence ------------------------------------------------------------


async def test_an_already_fetched_row_is_a_no_op(monkeypatch, admin_session, pending):
    """`rescan_stuck` and the delta sweep may both enqueue the same row."""
    tenant_id, _, row_id = pending
    await admin_session.execute(
        text("UPDATE email_messages SET processing_status = 'fetched' WHERE id = :id"),
        {"id": row_id},
    )
    await admin_session.commit()

    def _explode(request):
        raise AssertionError("Graph must not be called for an already-fetched row")

    _, queued = _wire(monkeypatch, _explode)

    await _run(pending)

    assert queued == []


async def test_a_job_naming_the_wrong_tenant_does_nothing(monkeypatch, pending):
    """The tenant travels in the job payload, so RLS is what validates the
    pair — a mismatched job reads no row and quietly does nothing."""
    _, mailbox_id, row_id = pending

    def _explode(request):
        raise AssertionError("Graph must not be called without a matching row")

    _, queued = _wire(monkeypatch, _explode)

    await jobs.fetch_email(
        {},
        email_message_id=str(row_id),
        tenant_id=str(uuid.uuid4()),
        mailbox_id=str(mailbox_id),
    )

    assert queued == []


async def test_an_unknown_row_does_nothing(monkeypatch, pending):
    tenant_id, mailbox_id, _ = pending

    def _explode(request):
        raise AssertionError("Graph must not be called for a row that does not exist")

    _, queued = _wire(monkeypatch, _explode)

    await jobs.fetch_email(
        {},
        email_message_id=str(uuid.uuid4()),
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )

    assert queued == []


async def test_a_retry_overwrites_its_own_object_rather_than_orphaning(
    monkeypatch, admin_session, pending
):
    """Keys are derived, so the second attempt lands on the first one's key."""
    tenant_id, mailbox_id, row_id = pending
    store, _ = _wire(monkeypatch, _ok)

    await _run(pending)
    await admin_session.execute(
        text("UPDATE email_messages SET processing_status = 'pending' WHERE id = :id"),
        {"id": row_id},
    )
    await admin_session.commit()
    await _run(pending)

    keys = [k for k in store.objects if k.startswith(f"{tenant_id}/{mailbox_id}/")]
    assert len(keys) == 2, "one text body and one html body, not four"
