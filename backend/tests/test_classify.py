"""The relevance gate (plan Task 4).

The gate's whole job is to decide what NOT to spend an extraction call on, so
the tests that matter are the ones about being wrong. Every failure mode here —
a broken model, a missing key, a killed worker — must end with the email still
reaching extraction, because a dropped job order is a vacancy the recruiter
never sees and never knows to look for.

allow-hardcode: the SQL and the model responses below are test fixtures.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.rls import tenant_session
from app.services.ingest import classify as classify_module
from app.services.ingest.classify import classify, should_extract
from app.services.llm.client import FakeLLM, LLMInvalidJSON
from app.services.storage.r2 import InMemoryBodyStore, body_key
from app.workers import jobs


@pytest.fixture(autouse=True)
def _configured_classifier(monkeypatch):
    """Give every test a model of its own.

    Without it these passed only where `CLASSIFIER_MODEL` happened to be set —
    and it is set nowhere: not in CI, not in the repo `.env`, not on the arq
    service. A test that depends on ambient configuration is testing the
    configuration. The model is never called; `complete_json` is faked.
    """
    monkeypatch.setattr(settings, "CLASSIFIER_MODEL", "test/classifier")
    # The gate runs on its own provider, not the extraction router.
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://gate.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")


# --- the verdict ------------------------------------------------------------


async def test_a_job_order_is_recruitment():
    llm = FakeLLM({"is_job_order": True, "reason": "describes a vacancy"})

    result = await classify("We need a QA Executive, $3,700-$4,500", llm=llm)

    assert result.status == "recruitment"


async def test_an_invoice_is_not_recruitment():
    llm = FakeLLM({"is_job_order": False, "reason": "an invoice"})

    result = await classify("Invoice 4432 attached, payment due 30 days", llm=llm)

    assert result.status == "non_recruitment"


async def test_the_email_text_reaches_the_prompt():
    """A gate that classifies an empty prompt would answer at random."""
    llm = FakeLLM({"is_job_order": True, "reason": "a vacancy"})

    await classify("We need a QA Executive", llm=llm)

    assert "We need a QA Executive" in llm.prompts[0]


async def test_the_model_comes_from_settings():
    """A model id in source is a defect (Global Constraints)."""
    seen = {}

    async def spy(prompt, *, model, schema, **_):
        seen["model"] = model
        return await FakeLLM({"is_job_order": True, "reason": "r"})(
            prompt, model=model, schema=schema
        )

    await classify("anything", llm=spy)

    assert seen["model"] == settings.CLASSIFIER_MODEL


# --- failing open -----------------------------------------------------------


async def test_a_model_failure_fails_open_to_uncertain():
    """Failing closed loses a job order silently; failing open costs one call."""

    async def broken(prompt, **kwargs):
        raise LLMInvalidJSON("garbage")

    result = await classify("anything", llm=broken)

    assert result.status == "uncertain"


async def test_a_missing_key_fails_open_rather_than_defaulting_to_false():
    llm = FakeLLM({"reason": "model forgot the verdict field"})

    result = await classify("anything", llm=llm)

    assert result.status == "uncertain"


async def test_uncertain_still_proceeds_to_extraction():
    assert should_extract("recruitment") is True
    assert should_extract("uncertain") is True
    assert should_extract("non_recruitment") is False


# --- the job ----------------------------------------------------------------


@pytest.fixture
async def fetched(admin_session):
    """A tenant, mailbox and one email row that has its body stored."""
    tenant_id, mailbox_id, row_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await admin_session.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'A', :slug)"),
        {"id": tenant_id, "slug": f"c-{tenant_id.hex[:8]}"},
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
            "INSERT INTO email_messages"
            " (id, tenant_id, mailbox_id, graph_message_id, subject, sender_email,"
            "  body_html_r2_key, processing_status, retention_until)"
            " VALUES (:id, :tenant, :mailbox, 'MSG-1', 'QA Executive',"
            "  'evelyn@example.com', :key, 'fetched', now() + interval '720 days')"
        ),
        {
            "id": row_id,
            "tenant": tenant_id,
            "mailbox": mailbox_id,
            "key": body_key(tenant_id, mailbox_id, "MSG-1", "html"),
        },
    )
    await admin_session.commit()
    yield tenant_id, mailbox_id, row_id
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


async def _run(fixture):
    tenant_id, mailbox_id, row_id = fixture
    await jobs.classify_email(
        {},
        email_message_id=str(row_id),
        tenant_id=str(tenant_id),
        mailbox_id=str(mailbox_id),
    )


async def _row(tenant_id, row_id):
    async with tenant_session(tenant_id) as session:
        return (
            await session.execute(
                text("SELECT * FROM email_messages WHERE id = :id"), {"id": row_id}
            )
        ).one()


async def test_a_recruitment_email_goes_on_to_extraction(monkeypatch, fetched):
    tenant_id, mailbox_id, row_id = fetched
    store, queued, _ = _wire(monkeypatch, {"is_job_order": True, "reason": "a vacancy"})
    await store.put(
        body_key(tenant_id, mailbox_id, "MSG-1", "html"), "<p>Up to $3500</p>"
    )

    await _run(fetched)

    row = await _row(tenant_id, row_id)
    assert row.classification_status == "recruitment"
    assert row.classification_model == settings.CLASSIFIER_MODEL
    assert row.classification_version == settings.PROMPT_VERSION
    assert queued == [
        (
            "extract_email",
            {
                "email_message_id": str(row_id),
                "tenant_id": str(tenant_id),
                "mailbox_id": str(mailbox_id),
            },
        )
    ]


async def test_the_classified_text_is_the_one_offsets_index_into(monkeypatch, fetched):
    """`to_text` is the single source of truth: the gate must not see raw HTML."""
    tenant_id, mailbox_id, row_id = fetched
    store, _, llm = _wire(monkeypatch, {"is_job_order": True, "reason": "a vacancy"})
    await store.put(
        body_key(tenant_id, mailbox_id, "MSG-1", "html"), "<p>Up to $3500</p>"
    )

    await _run(fetched)

    prompt = llm.prompts[0]
    assert "<p>" not in prompt
    assert "SUBJECT: QA Executive" in prompt
    assert "SENDER: evelyn@example.com" in prompt


async def test_a_non_recruitment_email_is_skipped_and_expires_sooner(
    monkeypatch, fetched
):
    tenant_id, mailbox_id, row_id = fetched
    store, queued, _ = _wire(monkeypatch, {"is_job_order": False, "reason": "invoice"})
    await store.put(body_key(tenant_id, mailbox_id, "MSG-1", "html"), "<p>Invoice</p>")
    before = (await _row(tenant_id, row_id)).retention_until

    await _run(fetched)

    row = await _row(tenant_id, row_id)
    assert row.classification_status == "non_recruitment"
    assert row.processing_status == "skipped"
    assert row.retention_until < before, "a message we keep nothing from must age out"
    assert queued == []


async def test_an_uncertain_verdict_still_extracts(monkeypatch, fetched):
    tenant_id, mailbox_id, row_id = fetched
    store, queued, _ = _wire(monkeypatch, {"reason": "no verdict field"})
    await store.put(body_key(tenant_id, mailbox_id, "MSG-1", "html"), "<p>?</p>")

    await _run(fetched)

    row = await _row(tenant_id, row_id)
    assert row.classification_status == "uncertain"
    assert [name for name, _ in queued] == ["extract_email"]


async def test_classify_resumes_a_row_left_at_classifying(
    monkeypatch, fetched, admin_session
):
    """A worker killed mid-classify must be recoverable, not stuck forever.

    `rescan_stuck` re-enqueues exactly this job for a row at `classifying`, so
    refusing anything but `fetched` would make that row retry until the heat
    death of the queue.
    """
    tenant_id, mailbox_id, row_id = fetched
    store, queued, _ = _wire(monkeypatch, {"is_job_order": False, "reason": "invoice"})
    await store.put(body_key(tenant_id, mailbox_id, "MSG-1", "html"), "<p>Invoice</p>")
    await admin_session.execute(
        text("UPDATE email_messages SET processing_status = 'classifying' WHERE id = :i"),
        {"i": row_id},
    )
    await admin_session.commit()

    await _run(fetched)

    row = await _row(tenant_id, row_id)
    assert row.processing_status != "classifying", "the row must have moved on"


async def test_an_already_classified_row_is_not_reclassified(monkeypatch, fetched):
    """Two producers may name the same row; paying twice for one verdict is waste."""
    tenant_id, mailbox_id, row_id = fetched
    store, queued, llm = _wire(monkeypatch)  # no responses: a call would raise
    async with tenant_session(tenant_id) as session:
        await session.execute(
            text("UPDATE email_messages SET processing_status = 'extracting'"
                 " WHERE id = :i"),
            {"i": row_id},
        )

    await _run(fetched)

    assert llm.prompts == []
    assert queued == []


async def test_a_body_store_outage_leaves_the_row_recoverable(monkeypatch, fetched):
    """The row must stay in a status `rescan_stuck` re-enqueues, not go terminal."""
    tenant_id, mailbox_id, row_id = fetched
    _wire(monkeypatch, {"is_job_order": True, "reason": "a vacancy"})

    class Broken:
        async def get(self, key):
            raise RuntimeError("bucket is gone")

    monkeypatch.setattr(jobs, "body_store", Broken)

    with pytest.raises(RuntimeError):
        await _run(fetched)

    row = await _row(tenant_id, row_id)
    assert row.processing_status == "classifying"


async def test_the_job_is_registered_with_the_worker():
    """The production failure this task fixes: arq logged 'function not found'."""
    from app.workers.settings import WorkerSettings

    assert jobs.classify_email in WorkerSettings.functions


async def test_an_unconfigured_classifier_refuses_rather_than_guessing():
    """The gate fails open, which is right — and dangerous when unconfigured.

    With no model set, every email would classify as `uncertain`, every one
    would go to extraction, and the system would look like it worked with a
    suspiciously indecisive model. `CLASSIFIER_MODEL` defaults to "" and was
    set nowhere in this deployment, so that was the live behaviour waiting to
    happen. Refusing loudly is the only honest answer.
    """
    import pytest

    from app.core.config import settings
    from app.workers import jobs

    original = settings.CLASSIFIER_MODEL
    settings.CLASSIFIER_MODEL = ""
    try:
        with pytest.raises(RuntimeError, match="no model configured"):
            await jobs.classify_email(
                None,
                email_message_id=str(uuid.uuid4()),
                tenant_id=str(uuid.uuid4()),
                mailbox_id=str(uuid.uuid4()),
            )
    finally:
        settings.CLASSIFIER_MODEL = original
