"""Body-hash classification dedupe (Phase 2 cost optimisation).

The same job order arrives more than once — a client sends it to two
recruiters at one agency, delta sync re-delivers a message, a reply chain
re-fetches an earlier email. Each copy used to pay the gate. With `body_hash`
stored at fetch time, a second email whose processed text is identical to one
already classified reuses the recorded verdict: zero model call, same answer.

Invariant: the dedupe fails open. A missing hash, a broken query, or no prior
verdict all send the email to the model exactly as before. And the dedupe must
never leak across tenants — RLS scopes the lookup to the tenant already.

allow-hardcode: the SQL and the model responses below are test fixtures.
"""

import hashlib
import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.ingest import classify as classify_module
from app.services.llm.client import FakeLLM
from app.services.storage.r2 import InMemoryBodyStore, body_key
from app.workers import jobs
from tests.test_classify_batch import _bodies, _row, _verdicts


@pytest.fixture(autouse=True)
def _configured_gate(monkeypatch):
    """Configuration of this test's own, never the repo `.env`."""
    monkeypatch.setattr(settings, "CLASSIFIER_MODEL", "test/classifier")
    monkeypatch.setattr(settings, "LLM_PROVIDER_BASE_URL", "https://gate.test/v1")
    monkeypatch.setattr(settings, "LLM_PROVIDER_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CLASSIFIER_BATCH_SIZE", 2)
    monkeypatch.setattr(settings, "CLASSIFIER_CHARS_PER_EMAIL", 50)
    monkeypatch.setattr(settings, "CLASSIFY_SWEEP_LIMIT", 100)


def _hash(body_text: str) -> str:
    return hashlib.sha256(body_text.encode("utf-8")).hexdigest()


@pytest.fixture
async def dupes(admin_session):
    """One tenant, mailbox, and two rows carrying the SAME body hash.

    Row 1 is already classified (the "first copy"); row 2 is unclassified and
    shares row 1's hash (the "duplicate"). Returns
    `(tenant_id, mailbox_id, {"prior": row1, "duplicate": row2})`.
    """
    tenant_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"d-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user-1', 'inbox', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    digest = _hash("We need a QA Executive, $3,700-$4,500")
    prior_id, duplicate_id = uuid.uuid4(), uuid.uuid4()
    # The already-classified row.
    await admin_session.execute(
        text(
            "INSERT INTO email_messages"
            " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email,"
            "  body_html_r2_key, body_hash, processing_status, classification_status,"
            "  classification_model, retention_until)"
            " VALUES (:id, :tenant, :mailbox, :gid, 'Re: Job', 'client@acme.com',"
            "  :key, :digest, 'classified', 'recruitment', 'test/classifier',"
            "  now() + interval '720 days')"
        ),
        {
            "id": prior_id,
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "gid": "PRIOR-1",
            "key": body_key(tenant_id, mailbox_id, "PRIOR-1", "html"),
            "digest": digest,
        },
    )
    # The duplicate, still unclassified.
    await admin_session.execute(
        text(
            "INSERT INTO email_messages"
            " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email,"
            "  body_html_r2_key, body_hash, processing_status, retention_until)"
            " VALUES (:id, :tenant, :mailbox, :gid, 'Fwd: Job', 'client@acme.com',"
            "  :key, :digest, 'classifying', now() + interval '720 days')"
        ),
        {
            "id": duplicate_id,
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "gid": "DUP-1",
            "key": body_key(tenant_id, mailbox_id, "DUP-1", "html"),
            "digest": digest,
        },
    )
    await admin_session.commit()
    yield tenant_id, mailbox_id, {"prior": prior_id, "duplicate": duplicate_id}
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id}
    )
    await admin_session.commit()


def _wire(monkeypatch, *responses):
    """Fake R2, fake model, captured queue. Nothing reaches the network."""
    store = InMemoryBodyStore()
    queued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        queued.append((name, kwargs))
        return True

    llm = FakeLLM(*responses)
    monkeypatch.setattr(jobs, "body_store", lambda: store)
    monkeypatch.setattr(jobs, "enqueue", _enqueue)
    monkeypatch.setattr(classify_module, "complete_json", llm)
    return store, queued, llm


async def test_a_duplicate_body_skips_the_model_call(monkeypatch, dupes):
    """The whole point: a second copy of an already-classified body pays zero
    tokens and inherits the first copy's verdict."""
    tenant_id, mailbox_id, ids = dupes
    # No queued responses: any model call would raise.
    store, queued, llm = _wire(monkeypatch)
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(ids["duplicate"])]
    )

    assert llm.prompts == []
    # A reused `recruitment` verdict proceeds to extraction.
    assert [kw["email_message_id"] for _, kw in queued] == [str(ids["duplicate"])]

    row = await _row(tenant_id, ids["duplicate"])
    assert row.classification_status == "recruitment"
    assert row.classification_model == "test/classifier"
    # The reused verdict carries the first copy's reason, tagged.
    assert "identical to an earlier email" in row.classification_reason


async def test_a_fresh_body_still_calls_the_model(monkeypatch, dupes, admin_session):
    """No prior verdict for the hash → the gate runs exactly as before."""
    tenant_id, mailbox_id, ids = dupes
    # Give the duplicate a genuinely different hash (a different body): no
    # prior verdict matches, so the gate runs.
    await admin_session.execute(
        text("UPDATE email_messages SET body_hash = :digest WHERE id = :id"),
        {"digest": _hash("A completely different body"), "id": ids["duplicate"]},
    )
    await admin_session.commit()

    store, queued, llm = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": True, "reason": "a vacancy"}),
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(ids["duplicate"])]
    )

    assert len(llm.prompts) == 1
    assert queued[0][0] == "extract_email"


async def test_a_duplicate_never_leaks_across_tenants(monkeypatch, dupes, admin_session):
    """A prior verdict in tenant A must not answer a duplicate in tenant B."""
    tenant_id, _, _ = dupes
    other_tenant, other_mailbox = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'B', :slug)"),
        {"id": other_tenant, "slug": f"o-{other_tenant.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user-2', 'inbox', 'folder', 24)"
        ),
        {"id": other_mailbox, "tenant": other_tenant},
    )
    digest = _hash("We need a QA Executive, $3,700-$4,500")
    other_row = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO email_messages"
            " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email,"
            "  body_html_r2_key, body_hash, processing_status, retention_until)"
            " VALUES (:id, :tenant, :mailbox, :gid, 'Fwd: Job', 'client@acme.com',"
            "  :key, :digest, 'classifying', now() + interval '720 days')"
        ),
        {
            "id": other_row,
            "tenant": other_tenant,
            "mailbox": other_mailbox,
            "gid": "OTHER-DUP",
            "key": body_key(other_tenant, other_mailbox, "OTHER-DUP", "html"),
            "digest": digest,
        },
    )
    await admin_session.commit()

    store, _, llm = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": True, "reason": "a vacancy"}),
    )
    await store.put(
        body_key(other_tenant, other_mailbox, "OTHER-DUP", "html"),
        "<p>We need a QA Executive</p>",
    )

    await jobs.classify_batch(
        {},
        tenant_id=str(other_tenant),
        email_message_ids=[str(other_row)],
    )

    # The other tenant's duplicate still went to the model — RLS hid tenant A's
    # verdict from the lookup.
    assert len(llm.prompts) == 1
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": other_tenant}
    )
    await admin_session.commit()


async def test_a_null_hash_falls_back_to_the_model(monkeypatch, dupes, admin_session):
    """A row with no body_hash cannot be deduped; the gate runs."""
    tenant_id, mailbox_id, ids = dupes
    # Null the duplicate's hash.
    await admin_session.execute(
        text("UPDATE email_messages SET body_hash = NULL WHERE id = :id"),
        {"id": ids["duplicate"]},
    )
    await admin_session.commit()

    store, queued, llm = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": True, "reason": "a vacancy"}),
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(ids["duplicate"])]
    )

    assert len(llm.prompts) == 1
    assert queued[0][0] == "extract_email"


async def test_the_single_email_recovery_path_reuses_too(monkeypatch, dupes):
    """`classify_email` (rescan recovery) applies the same dedupe."""
    tenant_id, mailbox_id, ids = dupes
    store, queued, llm = _wire(monkeypatch)
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_email(
        {},
        email_message_id=str(ids["duplicate"]),
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )

    assert llm.prompts == []
    assert [kw["email_message_id"] for _, kw in queued] == [str(ids["duplicate"])]
