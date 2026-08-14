"""The trusted-sender gate skip (Phase 1 cost optimisation).

The gate is the highest-volume LLM call in the system, and most job orders
arrive from domains the gate has already answered about. Once a domain has
earned a confident `recruitment` verdict, the next email from it should not
pay the gate again. These tests assert the two halves of that:

1. Trust is self-seeded from a confident verdict, and only from one.
2. A trusted email skips the model call, records a `trusted-sender` verdict,
   and still proceeds to extraction.

The invariant that must never break: the skip is fail-open. A missing trust
row, a broken table, or a malformed sender must all send the email to the
gate exactly as before — trust can only ever skip a call, never drop a job
order.

allow-hardcode: the SQL and the model responses below are test fixtures.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
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


@pytest.fixture
async def trusted(admin_session):
    """A tenant, mailbox and rows with a known sender domain.

    Returns `(tenant_id, mailbox_id, {"acme": <row1 id>, "example": <row2 id>})`
    so tests can address each row by the sender it carries, rather than by a
    sort order that depends on random UUIDs.
    """
    tenant_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"t-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user-1', 'inbox', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    rows: dict[str, uuid.UUID] = {}
    for n, sender in ((1, "client@acme.com"), (2, "evelyn@example.com")):
        row_id = uuid.uuid4()
        rows["acme" if n == 1 else "example"] = row_id
        await admin_session.execute(
            text(
                "INSERT INTO email_messages"
                " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email,"
                "  body_html_r2_key, processing_status, retention_until)"
                " VALUES (:id, :tenant, :mailbox, :gid, :subject, :sender,"
                "  :key, 'classifying', now() + interval '720 days')"
            ),
            {
                "id": row_id,
                "tenant": tenant_id,
                "mailbox": mailbox_id,
                "gid": f"MSG-{n}",
                "subject": f"Subject {n}",
                "sender": sender,
                "key": body_key(tenant_id, mailbox_id, f"MSG-{n}", "html"),
            },
        )
    await admin_session.commit()
    yield tenant_id, mailbox_id, rows
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


async def test_a_trusted_sender_skips_the_model_call(monkeypatch, trusted):
    """The whole point: an email from a trusted domain pays zero tokens.

    The trusted email (row 1, acme.com) must not reach the model; the
    untrusted one (row 2, example.com) still does — it gets the gate's
    verdict "an invoice", so it does not proceed to extraction. Only the
    trusted email is queued for extraction.
    """
    tenant_id, mailbox_id, rows = trusted
    store, queued, llm = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": False, "reason": "an invoice"}),
    )
    await _bodies(store, tenant_id, mailbox_id)

    # Seed trust for acme.com only.
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO trusted_senders (id, tenant_id, domain)"
                " VALUES (:id, :tenant, 'acme.com')"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id},
        )

    await jobs.classify_batch(
        {},
        tenant_id=str(tenant_id),
        email_message_ids=[str(i) for i in rows.values()],
    )

    # One model call, for the untrusted email only.
    assert len(llm.prompts) == 1
    assert "evelyn@example.com" in llm.prompts[0]
    assert "client@acme.com" not in llm.prompts[0]
    assert [kw["email_message_id"] for _, kw in queued] == [str(rows["acme"])]

    first = await _row(tenant_id, rows["acme"])
    assert first.classification_status == "recruitment"
    assert first.classification_model == jobs.TRUSTED_SENDER_MODEL


async def test_a_trusted_sender_records_zero_token_usage(monkeypatch, trusted):
    """The cost report must be able to see the free answers."""
    tenant_id, mailbox_id, rows = trusted
    store, _, _ = _wire(monkeypatch)
    await _bodies(store, tenant_id, mailbox_id)

    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO trusted_senders (id, tenant_id, domain)"
                " VALUES (:id, :tenant, 'acme.com')"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id},
        )

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(rows["acme"])]
    )

    async with tenant_session(tenant_id) as session:
        usage = (
            await session.execute(
                text(
                    "SELECT model_name, prompt_tokens, completion_tokens"
                    " FROM classification_usages WHERE email_message_id = :id"
                ),
                {"id": rows["acme"]},
            )
        ).one()
    assert usage.model_name == jobs.TRUSTED_SENDER_MODEL
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0


async def test_an_untrusted_sender_still_calls_the_model(monkeypatch, trusted):
    """No trust row → the gate runs exactly as before."""
    tenant_id, mailbox_id, rows = trusted
    store, queued, llm = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": True, "reason": "a vacancy"}),
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(rows["example"])]
    )

    assert len(llm.prompts) == 1
    assert queued[0][0] == "extract_email"


async def test_a_confident_verdict_seeds_trust(monkeypatch, trusted):
    """A `recruitment` verdict from the gate makes the next email free."""
    tenant_id, mailbox_id, rows = trusted
    # Only the untrusted sender (example.com) is in this batch, and the gate
    # says recruitment. That should seed example.com as trusted.
    store, _, llm = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": True, "reason": "a vacancy"}),
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(rows["example"])]
    )

    async with tenant_session(tenant_id) as session:
        trusted_domains = (
            await session.execute(
                text("SELECT domain FROM trusted_senders WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalars().all()
    assert "example.com" in trusted_domains


async def test_an_uncertain_verdict_does_not_seed_trust(monkeypatch, trusted):
    """`uncertain` is the gate failing open — trusting it would trust a
    domain the gate could not read."""
    tenant_id, mailbox_id, rows = trusted
    # The batch fake raises, which makes the email fail open to `uncertain`.
    async def broken(prompt, **kwargs):
        raise RuntimeError("gate down")

    store, _, _ = _wire(monkeypatch)
    monkeypatch.setattr(classify_module, "complete_json", broken)
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(rows["example"])]
    )

    async with tenant_session(tenant_id) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM trusted_senders WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()
    assert count == 0


async def test_a_non_recruitment_verdict_does_not_seed_trust(monkeypatch, trusted):
    """An invoice domain is not a trusted job-order domain."""
    tenant_id, mailbox_id, rows = trusted
    store, _, _ = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": False, "reason": "an invoice"}),
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(rows["example"])]
    )

    async with tenant_session(tenant_id) as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM trusted_senders WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar_one()
    assert count == 0


async def test_trust_never_leaks_across_tenants(monkeypatch, trusted, admin_session):
    """A trusted domain in tenant A must not skip the gate in tenant B."""
    tenant_id, _, _ = trusted
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
    other_row = uuid.uuid4()
    await admin_session.execute(
        text(
            "INSERT INTO email_messages"
            " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email,"
            "  body_html_r2_key, processing_status, retention_until)"
            " VALUES (:id, :tenant, :mailbox, :gid, 'Subject', 'client@acme.com',"
            "  :key, 'classifying', now() + interval '720 days')"
        ),
        {
            "id": other_row,
            "tenant": other_tenant,
            "mailbox": other_mailbox,
            "gid": "OTHER-1",
            "key": body_key(other_tenant, other_mailbox, "OTHER-1", "html"),
        },
    )
    await admin_session.commit()

    # Seed trust in tenant A only.
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text(
                "INSERT INTO trusted_senders (id, tenant_id, domain)"
                " VALUES (:id, :tenant, 'acme.com')"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id},
        )

    store, _, llm = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": True, "reason": "a vacancy"}),
    )
    await store.put(
        body_key(other_tenant, other_mailbox, "OTHER-1", "html"), "<p>Body</p>"
    )

    await jobs.classify_batch(
        {},
        tenant_id=str(other_tenant),
        email_message_ids=[str(other_row)],
    )

    # The other tenant's email from the same domain still went to the model.
    assert len(llm.prompts) == 1
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :id"), {"id": other_tenant}
    )
    await admin_session.commit()
