# allow-hardcode: the model responses, employer names and SQL below are test
# fixture content, not a matching or scoring oracle.
"""The job that reads an uploaded CV, and the sweep that finds a stranded one.

No test here reaches a model or the network: `extract_cv` is faked and the
body store is the in-memory double.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models.candidate import CandidateDocument
from app.services.llm.client import LLMResult
from app.services.storage.r2 import InMemoryBodyStore
from app.workers import cv_jobs, tasks
from tests.conftest import AdminSessionLocal
from tests.test_candidate_roles_api import _a_candidate_row, agency  # noqa: F401
from tests.test_cv_text import _pdf_with_text_pages

LINE = "Staff Nurse at Parkway Shenton from Mar 2019 to Mar 2020"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """The job refuses to touch a row before a model is configured, so every
    test supplies one. Nothing here ever calls it."""
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://cerebras.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


@pytest.fixture
def store(monkeypatch) -> InMemoryBodyStore:
    double = InMemoryBodyStore()
    monkeypatch.setattr(cv_jobs, "body_store", lambda: double)
    return double


def _fake_extraction(payload: dict):
    async def _extract(source, **kwargs):
        from app.services.cv.schema import CVResponse

        return CVResponse.model_validate(payload), LLMResult(
            data=payload, model="test/fast", latency_ms=5, raw={}
        )

    return _extract


def _one_role(source: str) -> dict:
    at = source.find("Staff Nurse")
    def field(value):
        start = source.find(value)
        return {
            "value": value,
            "evidence": value,
            "start_char": start,
            "end_char": start + len(value),
            "confidence": 0.9,
        }

    assert at >= 0
    return {
        "roles": [
            {
                "title": field("Staff Nurse"),
                "company": field("Parkway Shenton"),
                "start_date": {**field("Mar 2019"), "precision": "month"},
                "end_date": {**field("Mar 2020"), "precision": "month"},
            }
        ],
        "skills": [],
    }


async def _seed(tenant_id, candidate_id, store, data: bytes, state="pending"):
    document_id = uuid.uuid4()
    key = f"{tenant_id}/documents/{document_id}.pdf"
    if data is not None:
        await store.put_bytes(key, data, "application/pdf")
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_documents (id, tenant_id, candidate_id, filename,"
                " content_type, byte_size, object_key, parse_state)"
                " VALUES (:i, :t, :c, 'cv.pdf', 'application/pdf', :b, :k, :s)"
            ),
            {
                "i": document_id,
                "t": tenant_id,
                "c": candidate_id,
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
                    "SELECT parse_state, parse_error, text_key, text_chars"
                    " FROM candidate_documents WHERE id = :i"
                ),
                {"i": document_id},
            )
        ).one()


async def _cleanup(tenant_id):
    async with AdminSessionLocal() as s:
        for table in ("extraction_evidence", "extractions", "candidate_documents"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        await s.commit()


@pytest.mark.asyncio
async def test_a_readable_cv_becomes_roles_and_stored_text(agency, store, monkeypatch):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(tenant_id, candidate_id, store, _pdf_with_text_pages(1, LINE))

    captured: dict = {}

    async def _extract(source, **kwargs):
        captured["source"] = source
        return await _fake_extraction(_one_role(source))(source)

    monkeypatch.setattr(cv_jobs, "extract_cv", _extract)
    await cv_jobs.parse_candidate_cv(
        None,
        tenant_id=str(tenant_id),
        candidate_id=str(candidate_id),
        document_id=str(document_id),
    )

    row = await _document(document_id)
    assert row.parse_state == "parsed"
    assert row.text_chars == len(captured["source"])
    # Stored, not re-derived: every evidence offset indexes into this string.
    assert await store.get(row.text_key) == captured["source"]

    async with AdminSessionLocal() as s:
        roles = (
            await s.execute(
                text("SELECT source, status FROM candidate_roles WHERE candidate_id = :c"),
                {"c": candidate_id},
            )
        ).all()
    assert [(r.source, r.status) for r in roles] == [("cv_upload", "unconfirmed")]
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_file_that_is_not_a_document_is_unreadable_and_not_retried(agency, store):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(tenant_id, candidate_id, store, b"this is not a PDF at all")

    await cv_jobs.parse_candidate_cv(
        None,
        tenant_id=str(tenant_id),
        candidate_id=str(candidate_id),
        document_id=str(document_id),
    )

    row = await _document(document_id)
    # Terminal, and it says why in a sentence a recruiter can act on.
    assert row.parse_state == "unreadable"
    assert row.parse_error
    assert row.text_key is None
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_replaying_the_job_on_a_parsed_document_inserts_nothing(agency, store, monkeypatch):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(
        tenant_id, candidate_id, store, _pdf_with_text_pages(1, LINE), state="parsed"
    )

    async def _never(source, **kwargs):
        raise AssertionError("a parsed document must not be read again")

    monkeypatch.setattr(cv_jobs, "extract_cv", _never)
    await cv_jobs.parse_candidate_cv(
        None,
        tenant_id=str(tenant_id),
        candidate_id=str(candidate_id),
        document_id=str(document_id),
    )

    async with AdminSessionLocal() as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM candidate_roles WHERE candidate_id = :c"),
                {"c": candidate_id},
            )
        ).scalar_one()
    assert count == 0
    assert (await _document(document_id)).parse_state == "parsed"
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_the_stuck_sweep_requeues_a_document_stranded_in_pending(agency, store, monkeypatch):  # noqa: F811
    """A worker that dies between the upload committing and the job running
    strands the CV forever without this — the case a False from enqueue()
    does not cover."""
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(tenant_id, candidate_id, store, b"%PDF-1.4 ")
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE candidate_documents SET updated_at = now() - interval '2 hours'"
                " WHERE id = :i"
            ),
            {"i": document_id},
        )
        await s.commit()

    enqueued: list[tuple[str, dict]] = []

    async def _enqueue(name, **kwargs):
        enqueued.append((name, kwargs))
        return True

    monkeypatch.setattr(tasks, "enqueue", _enqueue)
    requeued = await tasks.rescan_stuck()

    mine = [
        kwargs
        for name, kwargs in enqueued
        if name == "parse_candidate_cv" and kwargs["document_id"] == str(document_id)
    ]
    assert mine == [
        {
            "tenant_id": str(tenant_id),
            "candidate_id": str(candidate_id),
            "document_id": str(document_id),
        }
    ]
    # Counted alongside the email rows rather than reported separately.
    assert requeued >= 1
    await _cleanup(tenant_id)


def test_the_job_is_registered_with_a_timeout_under_the_name_producers_use():
    """Both halves matter and neither is visible in production until too late.

    An unregistered name is an error inside arq, on the far side of the queue,
    where the producer already saw success. A registration with no timeout
    lets one hostile page hold a worker slot indefinitely — `text.py` bounds
    DOCX inflation, but a single-page FlateDecode bomb inflates inside pypdf
    where nothing of ours is watching.
    """
    from app.workers.settings import WorkerSettings

    registered = {
        (getattr(fn, "name", None) or fn.__name__): fn for fn in WorkerSettings.functions
    }
    assert "parse_candidate_cv" in registered
    assert registered["parse_candidate_cv"].timeout_s == settings.CV_PARSE_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_a_document_the_store_has_lost_is_unreadable(agency, store):  # noqa: F811
    tenant_id, user_id = agency
    candidate_id = await _a_candidate_row(tenant_id, user_id)
    document_id = await _seed(tenant_id, candidate_id, store, None)

    await cv_jobs.parse_candidate_cv(
        None,
        tenant_id=str(tenant_id),
        candidate_id=str(candidate_id),
        document_id=str(document_id),
    )

    assert (await _document(document_id)).parse_state == CandidateDocument.UNREADABLE
    await _cleanup(tenant_id)
