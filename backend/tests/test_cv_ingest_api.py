# allow-hardcode: "Jane Tan" / agency names below are test fixture content.
"""Uploading a CV with no candidate named — the ingest entry point.

The sibling per-candidate route (`POST /candidates/{id}/documents`) requires a
candidate id because the recruiter already knew who the CV belonged to. This
route does not: the platform reads the document's identity and resolves the
candidate itself. The boundary questions are the same adversarial ones — wrong
type, wrong size, exhausted quota, another agency's row — plus the one this
route exists for: a placeholder candidate is created to hold the foreign key,
and the ingest job (not the route) re-binds the document to the resolved person.
"""

import io
import uuid
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api import candidate_documents
from app.core.config import settings
from app.main import app
from app.models.candidate import CandidateDocument
from app.services.storage.r2 import InMemoryBodyStore
from tests.conftest import AdminSessionLocal
from tests.test_clients_api import sign_in  # the real session cookie, not a copy


async def _seed_agency() -> tuple[uuid.UUID, uuid.UUID]:
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.commit()
    return tid, uid


async def _drop_agency(tid: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        for table in (
            "extractions",
            "candidate_documents",
            "candidate_roles",
            "candidate_field_overrides",
            "candidate_skills",
            "candidates",
            "users",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


@pytest.fixture
async def agency():
    tid, uid = await _seed_agency()
    yield tid, uid
    await _drop_agency(tid)


@pytest.fixture
async def store():
    """The object store, swapped for the in-memory double."""
    double = InMemoryBodyStore()
    app.dependency_overrides[candidate_documents.body_store] = lambda: double
    yield double
    app.dependency_overrides.pop(candidate_documents.body_store, None)


@pytest.fixture
async def queued(monkeypatch):
    """Every job the route tried to enqueue. Redis is never touched."""
    jobs: list[tuple[str, dict]] = []

    async def _enqueue(name: str, **kwargs) -> bool:
        jobs.append((name, kwargs))
        return True

    monkeypatch.setattr(candidate_documents, "enqueue", _enqueue)
    return jobs


def _client_for(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(http, uid, tid)
    return http


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%%EOF\n"


def _docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def _upload_no_candidate(http: AsyncClient, content: bytes, **kwargs):
    return await http.post(
        "/api/candidates/documents",
        files={
            "file": (
                kwargs.get("name", "cv.pdf"),
                content,
                kwargs.get("type", "application/pdf"),
            )
        },
    )


async def _document_row(document_id: uuid.UUID):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT parse_state, origin, candidate_id FROM candidate_documents "
                    "WHERE id = :i"
                ),
                {"i": document_id},
            )
        ).one_or_none()


async def _candidate_count(tid: uuid.UUID) -> int:
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM candidates WHERE tenant_id = :t"), {"t": tid}
            )
        ).scalar_one()


# --- Upload -----------------------------------------------------------------


async def test_upload_creates_placeholder_and_enqueues_ingest(agency, store, queued):
    """The route's whole job: store bytes, hold the FK, enqueue the ingest job."""
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload_no_candidate(http, _pdf_bytes())

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["parse_state"] == CandidateDocument.INGEST_PENDING
    assert body["filename"] == "cv.pdf"

    # The ingest job is enqueued, not the parse job — identity must resolve first.
    assert len(queued) == 1
    name, kwargs = queued[0]
    assert name == "ingest_candidate_cv"
    assert kwargs == {
        "tenant_id": str(tid),
        "document_id": body["id"],
    }

    # A placeholder candidate holds the NOT NULL foreign key.
    row = await _document_row(uuid.UUID(body["id"]))
    assert row.parse_state == CandidateDocument.INGEST_PENDING
    assert row.origin == CandidateDocument.INGEST
    assert row.candidate_id is not None
    assert await _candidate_count(tid) == 1
    assert store.binary_objects  # bytes really were written


async def test_upload_accepts_a_docx(agency, store, queued):
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload_no_candidate(http, _docx_bytes(), name="cv.docx")
    assert response.status_code == 202, response.text


async def test_a_png_named_pdf_is_415(agency, store, queued):
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload_no_candidate(http, _png_bytes())
    assert response.status_code == 415
    assert queued == []
    assert await _candidate_count(tid) == 0


async def test_oversized_upload_is_413(agency, store, queued, monkeypatch):
    tid, uid = agency
    monkeypatch.setattr(settings, "CV_MAX_UPLOAD_BYTES", 512)
    async with _client_for(tid, uid) as http:
        response = await _upload_no_candidate(http, b"%PDF-1.4\n" + b"%" * 2048 + b"\n%%EOF\n")
    assert response.status_code == 413
    assert queued == []
    assert await _candidate_count(tid) == 0


async def test_past_the_daily_quota_is_429_with_nothing_stored(
    agency, store, queued, monkeypatch
):
    """The quota is shared with the per-candidate path — one budget per tenant per day."""
    tid, uid = agency
    monkeypatch.setattr(settings, "CV_DAILY_PARSE_QUOTA", 1)

    async with _client_for(tid, uid) as http:
        first = await _upload_no_candidate(http, _pdf_bytes())
        assert first.status_code == 202, first.text
        stored_after_first = dict(store.binary_objects)

        second = await _upload_no_candidate(http, _pdf_bytes())

    assert second.status_code == 429
    assert await _candidate_count(tid) == 1  # only the placeholder from the first
    assert dict(store.binary_objects) == stored_after_first
    assert len(queued) == 1


async def test_a_failed_enqueue_returns_503_and_leaves_no_ghost(agency, store, monkeypatch):
    """A lost enqueue on the no-candidate path is rolled back, not hidden.

    The per-candidate path keeps a FAILED row because the panel renders it and
    offers a retry. This path has no panel: the row would sit on a placeholder
    candidate the list hides, invisible and unretryable — a ghost that burns
    quota. So the whole upload — bytes, document row, placeholder — is undone
    and the caller gets an error it can show, rather than a promise that a
    candidate is on its way.
    """
    tid, uid = agency

    async def _refuse(name: str, **kwargs) -> bool:
        return False

    monkeypatch.setattr(candidate_documents, "enqueue", _refuse)

    async with _client_for(tid, uid) as http:
        response = await _upload_no_candidate(http, _pdf_bytes())

    assert response.status_code == 503, response.text
    assert "again" in response.json()["detail"].lower()
    # Nothing durable was left behind: no document row, no placeholder
    # candidate, no bytes in storage.
    assert await _candidate_count(tid) == 0
    assert not store.binary_objects
