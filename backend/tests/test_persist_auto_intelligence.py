"""Ingestion starts the Job Intelligence analysis automatically.

A fresh, clean, still-visible opportunity that survives `persist()` gets a
`pending` analysis row and an arq job on the interactive queue — the same
queue the Run button uses, charged against the same daily allowance. The
gates under test:

- `needs_review` extractions are skipped: suspect input is not worth 4-5
  model calls before a human has verified it.
- `persist()` still succeeds when the queue is down or the quota is gone —
  the email was ingested; an unanalysed job order costs only the click the
  recruiter always had.
- Re-runs of the same email (the deterministic-id retry path) do not start
  anything: the first run already analysed (or will analyse) that row.

The queue call is monkeypatched exactly like `test_notify_producer.py`
fakes `enqueue_deliveries`; no Redis is touched.

allow-hardcode: the SQL and fixture values below are test fixtures.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services.ingest import persist as persist_module
from app.services.ingest.persist import persist as persist_fn
from app.services.ingest.schema import ExtractionResponse
from app.services.llm.client import LLMResult
from app.workers import queue as queue_module

# Depends on committed rows surviving between the transaction's commit and
# its reads, and persist()'s auto-run reaches toward Redis (faked here).
# Other files' unscoped teardowns delete shared tables concurrently under
# xdist, so this joins the serial class f48cc82 established.
pytestmark = pytest.mark.serial

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
            "key": f"{tenant_id}/{mailbox_id}/MSG-1/html",
        },
    )
    await admin_session.commit()
    yield tenant_id, mailbox_id, row_id
    await admin_session.execute(
        text("DELETE FROM tenants WHERE id = :i"), {"i": tenant_id}
    )
    await admin_session.commit()


@pytest.fixture(autouse=True)
def _capture_queue(monkeypatch):
    """Capture both queue calls persist() can make.

    `enqueue_deliveries` (notifications) and the module-level `enqueue`
    (analysis jobs) both shell out to Redis after persist()'s transaction
    closes. Faking them keeps these tests off Redis and hands each test the
    list of enqueued analysis jobs to assert on.
    """
    captured = {"jobs": []}

    async def _noop_deliveries(tenant_id, delivery_ids):
        return None

    async def _capture_enqueue(job_name: str, **kwargs) -> bool:
        captured["jobs"].append({"name": job_name, **kwargs})
        return True

    monkeypatch.setattr(persist_module, "enqueue_deliveries", _noop_deliveries)
    # persist() imports `enqueue` inside the function (worker modules stay out
    # of the service's import path), so patch it at its home module.
    monkeypatch.setattr(queue_module, "enqueue", _capture_enqueue)
    captured["deliveries"] = []
    return captured


async def _row(admin_session, table: str, where: str, params):
    return (
        await admin_session.execute(
            text(f"SELECT * FROM {table} WHERE {where}"), params
        )
    ).mappings().one_or_none()


async def test_a_fresh_clean_opportunity_starts_an_analysis_automatically(
    admin_session, email_row, _capture_queue
) -> None:
    tenant_id, _, eid = email_row
    ids = await persist_fn(
        tenant_id, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE
    )
    assert len(ids) == 1

    # One analysis job went to the interactive queue for this opportunity.
    jobs = [
        j
        for j in _capture_queue["jobs"]
        if j["opportunity_id"] == str(ids[0])
    ]
    assert len(jobs) == 1
    assert jobs[0]["name"] == "run_job_intelligence"
    assert jobs[0]["tenant_id"] == str(tenant_id)

    # The pending row exists and belongs to this tenant.
    row = await _row(
        admin_session,
        "job_intelligence",
        "opportunity_id = :o",
        {"o": ids[0]},
    )
    assert row is not None
    assert row["state"] == "pending"


async def test_a_needs_review_extraction_gets_no_analysis(
    admin_session, email_row, _capture_queue
) -> None:
    """Suspect input is not worth 4-5 model calls before a human verifies it."""
    thin = _response(
        job_title={
            "value": "Finance officer",
            # An evidence span nowhere in the source fails verify() and demotes
            # the extraction to needs_review — the same rule the review event
            # tests use.
            "evidence": "this text does not appear in the source email",
            "start_char": 0,
            "end_char": 15,
            "confidence": 0.95,
        }
    )
    tenant_id, _, eid = email_row
    ids = await persist_fn(
        tenant_id, eid, thin, LLMResult(data={}, model="test/fast"), SOURCE
    )
    assert len(ids) == 1
    assert _capture_queue["jobs"] == []


async def test_over_quota_skips_the_auto_analysis_and_still_ingests(
    admin_session, email_row, _capture_queue, monkeypatch
) -> None:
    """Quota exhausted: no analysis starts, nothing raises — ingestion already
    succeeded and must never be dragged down by the analysis spend gate."""
    monkeypatch.setattr(settings, "INTELLIGENCE_DAILY_QUOTA", 0)
    tenant_id, _, eid = email_row
    ids = await persist_fn(
        tenant_id, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE
    )
    assert len(ids) == 1, "the opportunity itself must still be written"
    assert _capture_queue["jobs"] == []
    row = await _row(admin_session, "opportunities", "id = :i", {"i": ids[0]})
    assert row is not None


async def test_a_failing_enqueue_marks_the_row_failed_not_the_extraction(
    admin_session, email_row, _capture_queue, monkeypatch
) -> None:
    """Queue down at enqueue time: the pending row is parked `failed` so no
    panel spins forever, and persist() still returns normally."""

    async def _down(job_name: str, **kwargs) -> bool:
        if job_name == "run_job_intelligence":
            return False
        return True

    monkeypatch.setattr(queue_module, "enqueue", _down)
    tenant_id, _, eid = email_row
    ids = await persist_fn(
        tenant_id, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE
    )
    assert len(ids) == 1
    row = await _row(
        admin_session, "job_intelligence", "opportunity_id = :o", {"o": ids[0]}
    )
    assert row is not None
    assert row["state"] == "failed"


async def test_a_retry_of_the_same_email_does_not_reauto_analyse(
    admin_session, email_row, _capture_queue
) -> None:
    """Crash-retry: the deterministic opportunity id collides with the first
    run's row (`inserted=False`), so no second analysis may start — the first
    run started one, and `rescan_stuck` re-enqueues *that* row when needed."""
    tenant_id, _, eid = email_row
    first = await persist_fn(
        tenant_id, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE
    )
    second = await persist_fn(
        tenant_id, eid, _response(), LLMResult(data={}, model="test/fast"), SOURCE
    )
    assert first == second, "the deterministic id makes a retry hit the same row"

    jobs = [
        j
        for j in _capture_queue["jobs"]
        if j.get("opportunity_id") == str(first[0])
    ]
    assert len(jobs) == 1, "a retry must not start a second analysis"
