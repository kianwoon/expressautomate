# allow-hardcode: the PDF bytes, vacancy text and model response below are test
# fixture content, not a matching oracle.
"""The job that reads an uploaded job-description file, and the sweep recovery.

No test here reaches a model or the network: `ingest.extract` is faked and the
body store is the in-memory double.
"""

import io
import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models import OpportunityDocument
from app.services.ingest.schema import ExtractionResponse
from app.services.llm.client import LLMResult
from app.services.storage.r2 import InMemoryBodyStore
from app.workers import opportunity_document_jobs
from tests.conftest import AdminSessionLocal
from tests.test_cv_text import _pdf_with_text_pages

LINE = (
    "We are hiring a Warehouse Assistant at Sunrise Logistics in Tuas. "
    "Salary $2,800/month, Mon-Fri 9am-6pm, 6-month contract."
)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """The job refuses to touch a row before a model is configured, so every
    test supplies one. Nothing here ever calls it."""
    monkeypatch.setattr(settings, "LLM_PROVIDER_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "LLM_PROVIDER_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


@pytest.fixture
def store(monkeypatch) -> InMemoryBodyStore:
    double = InMemoryBodyStore()
    monkeypatch.setattr(opportunity_document_jobs, "body_store", lambda: double)
    return double


def _fake_extraction(payload: dict):
    async def _extract(source, **kwargs):
        return ExtractionResponse.model_validate(payload), LLMResult(
            data=payload, model="test/fast", latency_ms=5, raw={}
        )

    return _extract


def _one_job(source: str) -> dict:
    def field(value):
        start = source.find(value)
        return {
            "value": value,
            "evidence": value,
            "start_char": start,
            "end_char": start + len(value),
            "confidence": 0.9,
        }

    return {
        "jobs": [
            {
                "company": field("Sunrise Logistics"),
                "job_title": field("Warehouse Assistant"),
                "job_description": field("hiring a Warehouse Assistant"),
                "requirements": None,
                "salary": field("$2,800/month"),
                "salary_min": field("2800"),
                "salary_max": field("2800"),
                "salary_period": field("month"),
                "working_hours": field("Mon-Fri 9am-6pm"),
                "work_arrangement": None,
                "employment_type": None,
                "duration": field("6-month contract"),
                "location": field("Tuas"),
                "skills": None,
            }
        ]
    }


async def _seed(tenant_id, store, data: bytes | None, state="pending") -> uuid.UUID:
    document_id = uuid.uuid4()
    key = f"{tenant_id}/opportunities/x/documents/{document_id}.pdf"
    if data is not None:
        await store.put_bytes(key, data, "application/pdf")
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO opportunity_documents (id, tenant_id, opportunity_id,"
                " filename, content_type, byte_size, object_key, extract_state)"
                " VALUES (:i, :t, NULL, 'job.pdf', 'application/pdf', :b, :k, :s)"
            ),
            {
                "i": document_id,
                "t": tenant_id,
                "b": len(data or b""),
                "k": key,
                "s": state,
            },
        )
        await s.commit()
    return document_id


async def _document(document_id):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT extract_state, extract_error, prefill"
                    " FROM opportunity_documents WHERE id = :i"
                ),
                {"i": document_id},
            )
        ).one()


async def _cleanup(tenant_id):
    async with AdminSessionLocal() as s:
        await s.execute(
            text("DELETE FROM opportunity_documents WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
        await s.commit()


@pytest.fixture
async def agency():
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tenant_id, "n": f"agency-{tenant_id.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": user_id, "t": tenant_id, "e": f"u{user_id.hex[:6]}@agency.sg"},
        )
        await s.commit()
    yield tenant_id
    await _cleanup(tenant_id)
    async with AdminSessionLocal() as s:
        await s.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": tenant_id})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await s.commit()


@pytest.mark.asyncio
async def test_a_readable_document_becomes_prefill(agency, store, monkeypatch):
    tenant_id = agency
    source = _pdf_with_text_pages(1, LINE)
    document_id = await _seed(tenant_id, store, source)
    monkeypatch.setattr(
        opportunity_document_jobs,
        "extract",
        _fake_extraction(_one_job(LINE)),
    )

    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    state, error, prefill = await _document(document_id)
    assert state == OpportunityDocument.EXTRACTED
    assert error is None
    assert prefill == {
        "job_title_raw": "Warehouse Assistant",
        "company_name_raw": "Sunrise Logistics",
        "location_raw": "Tuas",
        "salary_raw": "$2,800/month",
        "working_hours_raw": "Mon-Fri 9am-6pm",
        "duration_raw": "6-month contract",
        "employment_type": None,
        "job_description": "hiring a Warehouse Assistant",
        "requirements": None,
    }


@pytest.mark.asyncio
async def test_an_empty_text_document_is_unreadable(agency, store, monkeypatch):
    tenant_id = agency
    # A structurally valid PDF whose single page carries no text at all.
    document_id = await _seed(tenant_id, store, _pdf_with_text_pages(1, ""))

    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    state, error, prefill = await _document(document_id)
    assert state == OpportunityDocument.UNREADABLE
    assert "scan" in error
    assert prefill is None


@pytest.mark.asyncio
async def test_a_missing_object_is_unreadable(agency, store, monkeypatch):
    tenant_id = agency
    document_id = await _seed(tenant_id, store, None)

    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    state, error, _ = await _document(document_id)
    assert state == OpportunityDocument.UNREADABLE
    assert "could not be found" in error


@pytest.mark.asyncio
async def test_a_model_failure_is_failed(agency, store, monkeypatch):
    tenant_id = agency
    document_id = await _seed(
        tenant_id, store, _pdf_with_text_pages(1, LINE)
    )
    from app.services.llm.client import LLMInvalidJSON

    async def _broken(source, **kwargs):
        raise LLMInvalidJSON("bad json")

    monkeypatch.setattr(opportunity_document_jobs, "extract", _broken)

    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    state, error, _ = await _document(document_id)
    assert state == OpportunityDocument.FAILED
    assert error


@pytest.mark.asyncio
async def test_a_document_with_no_vacancy_is_extracted_with_empty_prefill(
    agency, store, monkeypatch
):
    tenant_id = agency
    document_id = await _seed(
        tenant_id, store, _pdf_with_text_pages(1, LINE)
    )
    monkeypatch.setattr(
        opportunity_document_jobs,
        "extract",
        _fake_extraction({"jobs": []}),
    )

    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    state, error, prefill = await _document(document_id)
    assert state == OpportunityDocument.EXTRACTED
    assert prefill == {}


@pytest.mark.asyncio
async def test_a_doc_is_converted_before_extraction(agency, store, monkeypatch):
    """A `.doc` (OLE2) is stored as-is and converted to .docx inside the job."""
    tenant_id = agency
    # A real OLE2 .doc header; the conversion is stubbed because the test
    # machine has no LibreOffice guarantee. `sniff` returns None for these
    # bytes, which is exactly the branch that triggers conversion.
    doc_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128
    document_id = await _seed(tenant_id, store, doc_bytes)

    called: dict = {}

    # The gate is `CV_CONVERT_ENABLED AND converter_available()` — a binary
    # probe for LibreOffice. The probe is False on a CI runner (LibreOffice
    # lives only in the deployment image), so without pinning it this test
    # silently takes the `unreadable` branch on CI and the conversion stub is
    # never called. Pin it so the conversion path is exercised everywhere —
    # the same `_conversion_on` pattern as `test_cv_convert`.
    from app.services.cv import convert as _convert_module

    monkeypatch.setattr(settings, "CV_CONVERT_ENABLED", True)
    monkeypatch.setattr(_convert_module, "converter_available", lambda: True)

    async def _convert(data, *, kind):
        called["kind"] = kind
        return _pdf_with_text_pages(1, LINE), "pdf"

    monkeypatch.setattr(opportunity_document_jobs, "maybe_convert", _convert)
    monkeypatch.setattr(
        opportunity_document_jobs,
        "extract",
        _fake_extraction(_one_job(LINE)),
    )

    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    # `maybe_convert` receives kind None for OLE2 bytes — the job then uses
    # whatever kind conversion reports back.
    assert called["kind"] is None
    state, _, prefill = await _document(document_id)
    assert state == OpportunityDocument.EXTRACTED
    assert prefill["job_title_raw"] == "Warehouse Assistant"


def _docx_bytes() -> bytes:
    from docx import Document as DocxDocument

    doc = DocxDocument()
    doc.add_paragraph(LINE)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_a_doc_with_conversion_disabled_is_unreadable_not_stranded(
    agency, store, monkeypatch
):
    """A deployment without LibreOffice must park a .doc in `unreadable`,
    never leave it `extracting` for `rescan_stuck` to re-enqueue forever."""
    tenant_id = agency
    doc_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128
    document_id = await _seed(tenant_id, store, doc_bytes)
    monkeypatch.setattr(settings, "CV_CONVERT_ENABLED", False)

    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    state, error, _ = await _document(document_id)
    assert state == OpportunityDocument.UNREADABLE
    assert "legacy Word" in error


@pytest.mark.asyncio
async def test_a_docx_is_read_without_conversion(agency, store, monkeypatch):
    tenant_id = agency
    document_id = await _seed(tenant_id, store, _docx_bytes())
    monkeypatch.setattr(
        opportunity_document_jobs,
        "extract",
        _fake_extraction(_one_job(LINE)),
    )

    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    state, _, _ = await _document(document_id)
    assert state == OpportunityDocument.EXTRACTED


@pytest.mark.asyncio
async def test_a_document_past_the_attempt_ceiling_is_failed_not_retried(
    agency, store, monkeypatch
):
    """The cost guardrail: a job-description document that keeps timing out is
    re-enqueued by `rescan_stuck` forever — one billed extraction per sweep —
    unless the job itself refuses past a ceiling. This test makes the ceiling
    bind and asserts the model is never called again."""
    tenant_id = agency
    document_id = await _seed(
        tenant_id, store, _pdf_with_text_pages(1, LINE)
    )
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE opportunity_documents SET attempts = :a WHERE id = :i"
            ),
            {"a": settings.OPPORTUNITY_DOCUMENT_MAX_ATTEMPTS, "i": document_id},
        )
        await s.commit()

    async def _never(source, **kwargs):
        raise AssertionError(
            "a document past the attempt ceiling must not be extracted again"
        )

    monkeypatch.setattr(opportunity_document_jobs, "extract", _never)
    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    state, error, _ = await _document(document_id)
    assert state == OpportunityDocument.FAILED
    assert error


@pytest.mark.asyncio
async def test_a_provider_timeout_is_failed_not_escaped(
    agency, store, monkeypatch
):
    """A `TimeoutError` from the extraction client must park the row in
    `failed`, never escape to kill the job mid-flight and leave the row at
    `extracting` for `rescan_stuck` to re-enqueue (and re-bill) forever."""
    tenant_id = agency
    document_id = await _seed(
        tenant_id, store, _pdf_with_text_pages(1, LINE)
    )

    async def _timeout(source, **kwargs):
        raise TimeoutError("provider hung")

    monkeypatch.setattr(opportunity_document_jobs, "extract", _timeout)
    await opportunity_document_jobs.extract_opportunity_document(
        ctx=None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    state, error, _ = await _document(document_id)
    assert state == OpportunityDocument.FAILED
    assert error
