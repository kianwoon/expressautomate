"""Deterministic pre-gate noise rules (the "free gate").

The LLM gate is paid for every email that reaches it. But most of the
non-job-order mail a recruiter's inbox receives is *obviously* noise — a
newsletter, a no-reply notification, a job alert, an invoice — and a human
needs no model to know that. These rules answer the easy negatives before
the LLM gate is ever called.

The contract that keeps this safe:
  - A rule can answer `non_recruitment` and nothing else.
  - A rule can never answer `recruitment`.
  - A miss sends the email to the LLM gate exactly as before.

So the rules can only ever *subtract* from the LLM bill. They can never
fabricate a job order (which would be a wrong answer the product forbids)
and can never drop one (a missed rule just pays a few hundred tokens).

allow-hardcode: the SQL and the model responses below are test fixtures.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.services.ingest import classify as classify_module
from app.services.ingest import gate_rules
from app.services.llm.client import FakeLLM
from app.services.storage.r2 import InMemoryBodyStore, body_key
from app.workers import jobs
from tests.test_classify_batch import _bodies, _row, _verdicts


@pytest.fixture(autouse=True)
def _configured_gate(monkeypatch):
    """Configuration of this test's own, never the repo `.env`."""
    monkeypatch.setattr(settings, "CLASSIFIER_MODEL", "test/classifier")
    monkeypatch.setattr(settings, "DEEPSEEK_BASE_URL", "https://gate.test/v1")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CLASSIFIER_BATCH_SIZE", 2)
    monkeypatch.setattr(settings, "CLASSIFIER_CHARS_PER_EMAIL", 50)
    monkeypatch.setattr(settings, "CLASSIFY_SWEEP_LIMIT", 100)


@pytest.fixture
async def mailbox(admin_session):
    """One tenant and mailbox, and three unclassified rows:

    - `noise`  : subject "Invoice 4432 attached" from billing@acme.com
    - `noreply`: subject "Your invoice is ready" from no-reply@alerts.io
    - `fresh`  : subject "Senior Engineer opening" from hr@acme.com (no rule
                 fires — must reach the model)
    Returns `(tenant_id, mailbox_id, {"noise": id, "noreply": id, "fresh": id})`.
    """
    tenant_id, mailbox_id = uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"g-{tenant_id.hex[:8]}"},
    )
    await admin_session.execute(
        text(
            "INSERT INTO mailboxes"
            " (id, tenant_id, ms_user_id, folder_id, scope, retention_months)"
            " VALUES (:id, :tenant, 'ms-user-1', 'inbox', 'folder', 24)"
        ),
        {"id": mailbox_id, "tenant": tenant_id},
    )
    specs = {
        "noise": ("Invoice 4432 attached", "billing@acme.com"),
        "noreply": ("Your invoice is ready", "no-reply@alerts.io"),
        "fresh": ("Senior Engineer opening", "hr@acme.com"),
    }
    ids = {}
    for n, (name, (subject, sender)) in enumerate(specs.items(), start=1):
        row_id = uuid.uuid4()
        ids[name] = row_id
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
                "subject": subject,
                "sender": sender,
                "key": body_key(tenant_id, mailbox_id, f"MSG-{n}", "html"),
            },
        )
    await admin_session.commit()
    yield tenant_id, mailbox_id, ids
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


async def test_a_noise_subject_skips_the_model(monkeypatch, mailbox):
    """An obvious noise subject (invoice) pays zero tokens and is answered
    `non_recruitment` by the rule, so no extraction is queued."""
    tenant_id, mailbox_id, ids = mailbox
    # No queued model responses: a model call would raise.
    store, queued, llm = _wire(monkeypatch)
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(ids["noise"])]
    )

    assert llm.prompts == []
    assert queued == []  # non_recruitment → no extraction

    row = await _row(tenant_id, ids["noise"])
    assert row.classification_status == "non_recruitment"
    assert row.classification_model == jobs.GATE_RULE_MODEL


async def test_a_no_reply_sender_skips_the_model(monkeypatch, mailbox):
    """A no-reply sender local-part pays zero tokens."""
    tenant_id, mailbox_id, ids = mailbox
    store, queued, llm = _wire(monkeypatch)
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(ids["noreply"])]
    )

    assert llm.prompts == []
    assert queued == []

    row = await _row(tenant_id, ids["noreply"])
    assert row.classification_status == "non_recruitment"
    assert row.classification_model == jobs.GATE_RULE_MODEL


async def test_a_fresh_email_still_calls_the_model(monkeypatch, mailbox):
    """No rule fires → the LLM gate runs exactly as before."""
    tenant_id, mailbox_id, ids = mailbox
    store, queued, llm = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": True, "reason": "a vacancy"}),
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(ids["fresh"])]
    )

    assert len(llm.prompts) == 1
    assert queued[0][0] == "extract_email"


async def test_a_mixed_batch_pays_for_only_the_fresh_emails(monkeypatch, mailbox):
    """In one batch, noise + no-reply are free; only the fresh email pays."""
    tenant_id, mailbox_id, ids = mailbox
    store, queued, llm = _wire(
        monkeypatch,
        _verdicts({"index": 0, "is_job_order": True, "reason": "a vacancy"}),
    )
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {},
        tenant_id=str(tenant_id),
        email_message_ids=[str(ids["noise"]), str(ids["fresh"]), str(ids["noreply"])],
    )

    # One model call, for the fresh email only. (The 50-char truncation cuts
    # the sender line, but the fresh email's subject is present and the noise
    # rows are not.)
    assert len(llm.prompts) == 1
    assert "Senior Engineer opening" in llm.prompts[0]
    assert "Invoice" not in llm.prompts[0]
    assert "no-reply" not in llm.prompts[0]
    # Only the fresh email queues extraction.
    assert [kw["email_message_id"] for _, kw in queued] == [str(ids["fresh"])]

    # Verdicts as expected.
    for name in ("noise", "noreply"):
        row = await _row(tenant_id, ids[name])
        assert row.classification_status == "non_recruitment"
        assert row.classification_model == jobs.GATE_RULE_MODEL
    fresh = await _row(tenant_id, ids["fresh"])
    assert fresh.classification_status == "recruitment"
    assert fresh.classification_model == "test/classifier"


async def test_a_rule_verdict_records_zero_token_usage(monkeypatch, mailbox):
    """The cost report can see the rule-answered (free) verdicts."""
    tenant_id, mailbox_id, ids = mailbox
    store, _, _ = _wire(monkeypatch)
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_batch(
        {}, tenant_id=str(tenant_id), email_message_ids=[str(ids["noise"])]
    )

    async with tenant_session(tenant_id) as session:
        usage = (
            await session.execute(
                text(
                    "SELECT model_name, prompt_tokens, completion_tokens"
                    " FROM classification_usages WHERE email_message_id = :id"
                ),
                {"id": ids["noise"]},
            )
        ).one()
    assert usage.model_name == jobs.GATE_RULE_MODEL
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0


async def test_the_single_email_recovery_path_applies_rules_too(monkeypatch, mailbox):
    """`classify_email` (rescan recovery) applies the same rule filter."""
    tenant_id, mailbox_id, ids = mailbox
    store, queued, llm = _wire(monkeypatch)
    await _bodies(store, tenant_id, mailbox_id)

    await jobs.classify_email(
        {},
        email_message_id=str(ids["noise"]),
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )

    assert llm.prompts == []
    assert queued == []


def test_gate_rule_never_answers_recruitment():
    """The one-sided contract: rules say no, or say nothing. Never yes."""
    cases = [
        ("Senior Engineer opening", "hr@acme.com"),
        ("QA Executive needed, $3,700", "client@acme.com"),
        (None, "jane@acme.com"),
        ("", ""),
    ]
    for subject, sender in cases:
        assert gate_rules.gate_rule(subject=subject, sender_email=sender) is None


def test_gate_rule_catches_common_noise():
    assert (
        gate_rules.gate_rule(subject="Your daily job digest", sender_email="jobs@indeed.com")
        is not None
    )
    assert (
        gate_rules.gate_rule(subject="Invoice 4432 attached", sender_email="billing@acme.com")
        is not None
    )
    assert (
        gate_rules.gate_rule(
            subject="Unsubscribe from our newsletter", sender_email="mailer@sparkpostmail.com"
        )
        is not None
    )
    assert gate_rules.gate_rule(subject=None, sender_email="no-reply@alerts.io") is not None
    assert gate_rules.gate_rule(subject=None, sender_email="bounce@example.com") is not None
    assert (
        gate_rules.gate_rule(
            subject="You're invited to a meeting", sender_email="calendar@acme.com"
        )
        is not None
    )
