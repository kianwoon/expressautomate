# allow-hardcode: the model responses, OCR text and SQL below are test fixture
# content, not a matching or scoring oracle.
"""The OCR fallback for scanned CVs, wired into both parse paths.

No test here reaches Tesseract or the network: `ocr_extract` is monkeypatched to
a fake that returns canned text (or raises), and the feature is gated on
`CV_OCR_ENABLED` so the suite never depends on the toolchain being installed.

The property that matters most is the one the plan exists for: a scanned CV that
yields no text layer reaches `parsed` with roles when OCR recovers text, instead
of going terminal `unreadable`. The disabled-gate case preserves the original
behavior byte-for-byte, so a deployment that has not opted in is unchanged.
"""

import uuid

import pytest
from pypdf import PdfWriter
from sqlalchemy import text

from app.core.config import settings
from app.services.llm.client import LLMResult
from app.services.storage.r2 import InMemoryBodyStore
from app.workers import cv_jobs, ingest_jobs
from tests.conftest import AdminSessionLocal
from tests.test_candidate_roles_api import agency  # noqa: F401
from tests.test_cv_text import _pdf_with_text_pages

OCR_TEXT = (
    "Evelyn Tan\n"
    "evelyn.tan@example.com\n"
    "+65 9123 4567\n"
    "Senior Recruiter at KLN Logistics, Mar 2019 - Present.\n"
)


def _blank_pdf() -> bytes:
    """A valid PDF with a blank page — no text layer, like a scanned CV."""
    import io

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """The parse job refuses to touch a row before a model is configured."""
    monkeypatch.setattr(settings, "LLM_PROVIDER_BASE_URL", "https://deepseek.test/v1")
    monkeypatch.setattr(settings, "LLM_PROVIDER_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


@pytest.fixture
def store(monkeypatch) -> InMemoryBodyStore:
    double = InMemoryBodyStore()
    monkeypatch.setattr(cv_jobs, "body_store", lambda: double)
    return double


@pytest.fixture
def ocr_on(monkeypatch):
    """Turn OCR on without depending on the toolchain being installed.

    `ocr_configured()` ANDs `CV_OCR_ENABLED` with a binary probe; the probe is
    `ocr_available()` in `app.services.cv.ocr`, patched here so the suite never
    shells out to Tesseract. Setting the method on the instance is rejected by
    pydantic-settings (`ocr_configured` is a method, not a field), so the probe
    is the seam the gate reads through.
    """
    monkeypatch.setattr(settings, "CV_OCR_ENABLED", True)
    from app.services.cv import ocr as _ocr_module

    monkeypatch.setattr(_ocr_module, "ocr_available", lambda: True)


@pytest.fixture
def ingest_store(monkeypatch) -> InMemoryBodyStore:
    double = InMemoryBodyStore()
    monkeypatch.setattr(ingest_jobs, "body_store", lambda: double)
    return double


def _fake_ocr_that_returns(text: str):
    async def _ocr(data, *, languages, max_pages, timeout):
        return text

    return _ocr


def _fake_ocr_that_raises(exc: Exception):
    async def _ocr(data, *, languages, max_pages, timeout):
        raise exc

    return _ocr


def _one_role_payload(source: str) -> dict:
    """A CV-extraction payload quoting the OCR text, so the parse yields a role."""
    title = "Senior Recruiter"
    company = "KLN Logistics"
    start = source.find(title)
    cstart = source.find(company)

    def field(value: str, at: int) -> dict:
        return {
            "value": value,
            "evidence": value,
            "start_char": at,
            "end_char": at + len(value),
            "confidence": 0.9,
        }

    return {
        "roles": [
            {
                "title": field(title, start),
                "company": field(company, cstart),
                "start_date": {**field("Mar 2019", source.find("Mar 2019")), "precision": "month"},
                "end_date": {"value": "Not mentioned", "confidence": 0.0},
                "summary": {"value": "Not mentioned", "confidence": 0.0},
            }
        ],
        "skills": [],
    }


def _fake_extraction(payload: dict):
    async def _extract(source, **kwargs):
        from app.services.cv.schema import CVResponse

        return CVResponse.model_validate(payload), LLMResult(
            data=payload, model="test/fast", latency_ms=5, raw={}
        )

    return _extract


async def _seed(tenant_id, candidate_id, store, data: bytes):
    document_id = uuid.uuid4()
    key = f"{tenant_id}/documents/{document_id}.pdf"
    if data is not None:
        await store.put_bytes(key, data, "application/pdf")
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_documents (id, tenant_id, candidate_id, filename,"
                " content_type, byte_size, object_key, parse_state)"
                " VALUES (:i, :t, :c, 'cv.pdf', 'application/pdf', :b, :k, 'pending')"
            ),
            {"i": document_id, "t": tenant_id, "c": candidate_id, "b": len(data or b""), "k": key},
        )
        await s.commit()
    return document_id


async def _document(document_id):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text("SELECT parse_state, parse_error FROM candidate_documents WHERE id = :i"),
                {"i": document_id},
            )
        ).one()


async def _cleanup(tenant_id):
    async with AdminSessionLocal() as s:
        for table in ("extraction_evidence", "extractions", "candidate_documents"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        await s.commit()


# --- parse path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_scanned_pdf_with_ocr_yields_text_and_parses(
    agency, store, ocr_on, monkeypatch  # noqa: F811
):
    """The case the whole feature exists for: empty text layer → OCR → parsed."""
    from tests.test_candidate_roles_api import _a_candidate_row

    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(tenant_id, candidate_id, store, _blank_pdf())

    monkeypatch.setattr(cv_jobs, "ocr_extract", _fake_ocr_that_returns(OCR_TEXT))
    monkeypatch.setattr(cv_jobs, "extract_cv", _fake_extraction(_one_role_payload(OCR_TEXT)))

    await cv_jobs.parse_candidate_cv(
        None, tenant_id=str(tenant_id), candidate_id=str(candidate_id),
        document_id=str(document_id),
    )

    row = await _document(document_id)
    assert row.parse_state == "parsed"
    assert row.parse_error is None
    # The OCR'd text became the canonical source the parse indexed into.
    async with AdminSessionLocal() as s:
        roles = (
            await s.execute(
                text("SELECT title FROM candidate_roles WHERE candidate_id = :c"),
                {"c": candidate_id},
            )
        ).all()
    assert [r.title for r in roles] == ["Senior Recruiter"]
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_ocr_disabled_keeps_the_original_unreadable_path(
    agency, store  # noqa: F811
):
    """A deployment that has not opted in is unchanged: scanned → unreadable."""
    from tests.test_candidate_roles_api import _a_candidate_row

    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(tenant_id, candidate_id, store, _blank_pdf())

    # Gate off (the default) — no OCR, no dependency on the toolchain.
    await cv_jobs.parse_candidate_cv(
        None, tenant_id=str(tenant_id), candidate_id=str(candidate_id),
        document_id=str(document_id),
    )

    row = await _document(document_id)
    assert row.parse_state == "unreadable"
    assert "no text layer" in row.parse_error.lower()
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_ocr_that_yields_nothing_is_unreadable(agency, store, ocr_on, monkeypatch):  # noqa: F811
    """OCR ran and recovered nothing — a blank scan is still terminal."""
    from tests.test_candidate_roles_api import _a_candidate_row

    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(tenant_id, candidate_id, store, _blank_pdf())

    monkeypatch.setattr(cv_jobs, "ocr_extract", _fake_ocr_that_returns("   "))

    await cv_jobs.parse_candidate_cv(
        None, tenant_id=str(tenant_id), candidate_id=str(candidate_id),
        document_id=str(document_id),
    )

    row = await _document(document_id)
    assert row.parse_state == "unreadable"
    assert "even after ocr" in row.parse_error.lower()
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_ocr_unavailable_is_unreadable_with_the_cause(agency, store, ocr_on, monkeypatch):  # noqa: F811
    """A missing toolchain surfaces as `unreadable` naming OCR, not a crash."""
    from app.services.cv.ocr import OCRUnavailable
    from tests.test_candidate_roles_api import _a_candidate_row

    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(tenant_id, candidate_id, store, _blank_pdf())

    monkeypatch.setattr(
        cv_jobs, "ocr_extract", _fake_ocr_that_raises(OCRUnavailable("toolchain missing"))
    )

    await cv_jobs.parse_candidate_cv(
        None, tenant_id=str(tenant_id), candidate_id=str(candidate_id),
        document_id=str(document_id),
    )

    row = await _document(document_id)
    assert row.parse_state == "unreadable"
    assert "toolchain missing" in row.parse_error
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_text_pdf_never_invokes_ocr(agency, store, ocr_on, monkeypatch):  # noqa: F811
    """OCR is the fallback for empty text only; a digital CV skips it entirely."""
    from tests.test_candidate_roles_api import _a_candidate_row

    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(
        tenant_id, candidate_id, store, _pdf_with_text_pages(1, "Staff Nurse at Parkway Shenton")
    )

    async def _never(data, *, languages, max_pages, timeout):
        raise AssertionError("a text PDF must not reach OCR")

    monkeypatch.setattr(cv_jobs, "ocr_extract", _never)
    monkeypatch.setattr(
        cv_jobs, "extract_cv", _fake_extraction({"roles": [], "skills": []})
    )

    await cv_jobs.parse_candidate_cv(
        None, tenant_id=str(tenant_id), candidate_id=str(candidate_id),
        document_id=str(document_id),
    )

    # A text PDF parses (to empty, since the payload is empty) without OCR.
    assert (await _document(document_id)).parse_state in ("parsed", "empty")
    await _cleanup(tenant_id)
