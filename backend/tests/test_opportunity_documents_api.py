# allow-hardcode: the PDF/DOCX bytes, filenames and vacancy text below are test
# fixture content, not a matching oracle.
"""Uploading a job-description file, polling it, downloading it, deleting it.

Every test here is a boundary rather than a feature. The upload path takes
bytes from a browser, spends an agency's model budget on them and writes them
to shared object storage, so the questions worth asking are adversarial: can a
caller reach another agency's document, can they hand us a PNG named `.pdf`,
and does the create-dialog flow link the file to the vacancy it was read for.
"""

import io
import uuid
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api import opportunity_documents
from app.main import app
from app.models import OpportunityDocument
from app.services.storage.r2 import InMemoryBodyStore
from tests.conftest import AdminSessionLocal, sign_in


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
        for table in ("opportunity_documents", "opportunities", "users"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


@pytest.fixture
async def agency():
    tid, uid = await _seed_agency()
    yield tid, uid
    await _drop_agency(tid)


@pytest.fixture
async def other_agency():
    """A second agency, so "not yours" can be told apart from "not there"."""
    tid, uid = await _seed_agency()
    yield tid, uid
    await _drop_agency(tid)


@pytest.fixture
async def store():
    """The object store, swapped for the in-memory double.

    Through `app.dependency_overrides` rather than a patched global: the
    override is undone even when a test fails, so a failure cannot leave the
    next test writing to real R2.
    """
    double = InMemoryBodyStore()
    app.dependency_overrides[opportunity_documents.body_store] = lambda: double
    yield double
    app.dependency_overrides.pop(opportunity_documents.body_store, None)


@pytest.fixture
async def queued(monkeypatch):
    """Every job the upload route tried to enqueue. Redis is never touched."""
    jobs: list[tuple[str, dict]] = []

    async def _enqueue(name: str, **kwargs) -> bool:
        jobs.append((name, kwargs))
        return True

    monkeypatch.setattr(opportunity_documents, "enqueue", _enqueue)
    return jobs


def _client_for(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(http, uid, tid)
    return http


def _pdf_bytes(padding: int = 0) -> bytes:
    """Enough of a PDF for `sniff`. Nothing here ever parses it."""
    return b"%PDF-1.4\n" + b"%" * padding + b"\n%%EOF\n"


def _docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def _upload(http: AsyncClient, content: bytes, **kwargs):
    return await http.post(
        "/api/opportunities/documents",
        files={
            "file": (
                kwargs.get("name", "job.pdf"),
                content,
                kwargs.get("type", "application/pdf"),
            )
        },
    )


async def _document_rows(tid: uuid.UUID) -> list:
    async with AdminSessionLocal() as s:
        rows = await s.execute(
            text(
                "SELECT id, extract_state FROM opportunity_documents WHERE tenant_id = :t"
            ),
            {"t": tid},
        )
        return list(rows)


# --- Upload -----------------------------------------------------------------


async def test_upload_stores_enqueues_and_returns_201(agency, store, queued):
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, _pdf_bytes())
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["extract_state"] == OpportunityDocument.PENDING
        assert body["filename"] == "job.pdf"
        assert body["byte_size"] == len(_pdf_bytes())

    # One row, one enqueued job, and the bytes went to the object store.
    rows = await _document_rows(tid)
    assert len(rows) == 1
    assert rows[0][1] == OpportunityDocument.PENDING
    assert queued == [
        (
            "extract_opportunity_document",
            {"tenant_id": str(tid), "document_id": str(rows[0][0])},
        )
    ]
    assert len(store.binary_objects) == 1
    key = next(iter(store.binary_objects))
    assert key.startswith(f"{tid}/opportunities/")


async def test_upload_refuses_a_file_that_is_not_pdf_or_word(agency, store, queued):
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, _png_bytes(), name="cv.png", type="image/png")
    assert response.status_code == 415
    assert await _document_rows(tid) == []
    assert queued == []
    assert store.binary_objects == {}


async def test_upload_accepts_a_docx(agency, store, queued):
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(
            http,
            _docx_bytes(),
            name="job.docx",
            type="application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        )
    assert response.status_code == 201, response.text
    rows = await _document_rows(tid)
    assert len(rows) == 1


async def test_upload_marks_failed_when_enqueue_is_lost(agency, store, monkeypatch):
    tid, uid = agency

    async def _refuse(name: str, **kwargs) -> bool:
        return False

    monkeypatch.setattr(opportunity_documents, "enqueue", _refuse)
    async with _client_for(tid, uid) as http:
        response = await _upload(http, _pdf_bytes())
    assert response.status_code == 201, response.text
    assert response.json()["extract_state"] == OpportunityDocument.FAILED


# --- Isolation ---------------------------------------------------------------


async def test_another_agency_cannot_read_our_document(agency, other_agency, store, queued):
    tid, uid = agency
    other_tid, other_uid = other_agency
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]

    async with _client_for(other_tid, other_uid) as http:
        response = await http.get(f"/api/opportunities/documents/{document_id}")
    assert response.status_code == 404


async def test_another_agency_cannot_delete_our_document(agency, other_agency, store, queued):
    tid, uid = agency
    other_tid, other_uid = other_agency
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]

    async with _client_for(other_tid, other_uid) as http:
        response = await http.delete(f"/api/opportunities/documents/{document_id}")
    assert response.status_code == 404
    assert len(await _document_rows(tid)) == 1


# --- Poll --------------------------------------------------------------------


async def test_poll_returns_the_row_and_its_prefill(agency, store, queued):
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]
        # The worker would normally write the prefill; the test writes it the
        # way the job does, so the poll path is exercised against real state.
        async with AdminSessionLocal() as s:
            await s.execute(
                text(
                    "UPDATE opportunity_documents SET extract_state = 'extracted', "
                    " prefill = :p WHERE id = :i"
                ),
                {
                    "p": '{"job_title_raw": "Warehouse assistant"}',
                    "i": document_id,
                },
            )
            await s.commit()
        response = await http.get(f"/api/opportunities/documents/{document_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["extract_state"] == "extracted"
    assert body["prefill"]["job_title_raw"] == "Warehouse assistant"


# --- Link + download ---------------------------------------------------------


async def test_download_is_refused_until_linked_to_a_visible_opportunity(
    agency, store, queued
):
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]
        response = await http.get(f"/api/opportunities/documents/{document_id}/download")
    assert response.status_code == 404


async def test_delete_removes_the_object_then_the_row(agency, store, queued):
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]
        assert len(store.binary_objects) == 1
        response = await http.delete(f"/api/opportunities/documents/{document_id}")
        assert response.status_code == 204
    assert await _document_rows(tid) == []
    assert store.binary_objects == {}


async def test_double_delete_is_404_not_a_crash(agency, store, queued):
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]
        await http.delete(f"/api/opportunities/documents/{document_id}")
        response = await http.delete(f"/api/opportunities/documents/{document_id}")
    # A second delete of an already-removed row is 404, not a crash: the row
    # no longer exists, and the route is honest about it (the same as a CV's).
    assert response.status_code == 404


async def test_a_linked_document_cannot_be_deleted_by_a_read_only_share(
    agency, store, queued
):
    """Removing a vacancy's source file is an edit to the job order, so a
    colleague with only a read share gets 403, never a silent delete."""
    tid, uid = agency
    other_uid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role)"
                " VALUES (:i, :t, :e, 'recruiter')"
            ),
            {"i": other_uid, "t": tid, "e": f"u{other_uid.hex[:6]}@agency.sg"},
        )
        await s.commit()

    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]
        created = await http.post(
            "/api/opportunities",
            json={
                "client_id": None,
                "job_title_raw": "Warehouse assistant",
                "document_id": document_id,
            },
        )
        opportunity_id = created.json()["id"]

    # The second user is in the SAME agency, with only a read share.
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO opportunity_shares (id, tenant_id, opportunity_id,"
                " shared_by_user_id, shared_with_user_id, scope, note)"
                " VALUES (:i, :t, :o, :by, :with, 'user', 'read only')"
            ),
            {
                "i": uuid.uuid4(),
                "t": tid,
                "o": opportunity_id,
                "by": uid,
                "with": other_uid,
            },
        )
        await s.commit()

    async with _client_for(tid, other_uid) as http:
        response = await http.delete(f"/api/opportunities/documents/{document_id}")
    assert response.status_code == 403
    assert len(await _document_rows(tid)) == 1


async def test_a_linked_document_cannot_be_rebound_to_a_second_vacancy(
    agency, store, queued
):
    """Document ids travel in every payload, so a tenant member could name a
    colleague's already-linked file. The create route refuses it."""
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]
        first = await http.post(
            "/api/opportunities",
            json={
                "client_id": None,
                "job_title_raw": "First vacancy",
                "document_id": document_id,
            },
        )
        assert first.status_code == 201

        second = await http.post(
            "/api/opportunities",
            json={
                "client_id": None,
                "job_title_raw": "Second vacancy",
                "document_id": document_id,
            },
        )
    assert second.status_code == 422

    # The document is still attached to the first vacancy, not the second.
    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT opportunity_id FROM opportunity_documents WHERE id = :i"
                ),
                {"i": document_id},
            )
        ).one()
    assert str(row[0]) == first.json()["id"]


# --- Linking on save ---------------------------------------------------------


async def test_creating_a_job_order_links_the_document(agency, store, queued):
    """The create-dialog flow: upload, then save the vacancy carrying
    `document_id`, and the file travels with the row."""
    tid, uid = agency
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]

        response = await http.post(
            "/api/opportunities",
            json={
                "client_id": None,
                "job_title_raw": "Warehouse assistant",
                "company_name_raw": None,
                "document_id": document_id,
            },
        )
        assert response.status_code == 201, response.text
        opportunity_id = response.json()["id"]

    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT opportunity_id, extract_state FROM opportunity_documents"
                    " WHERE id = :i"
                ),
                {"i": document_id},
            )
        ).one()
    assert str(row[0]) == opportunity_id

    # The opportunity payload embeds the document.
    async with _client_for(tid, uid) as http:
        fetched = await http.get(f"/api/opportunities/{opportunity_id}")
    assert fetched.status_code == 200
    docs = fetched.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["id"] == document_id
    assert docs[0]["filename"] == "job.pdf"


async def test_creating_a_job_order_with_another_agencys_document_is_refused(
    agency, other_agency, store, queued
):
    tid, uid = agency
    other_tid, other_uid = other_agency
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, _pdf_bytes())
        document_id = uploaded.json()["id"]

    async with _client_for(other_tid, other_uid) as http:
        response = await http.post(
            "/api/opportunities",
            json={
                "client_id": None,
                "job_title_raw": "Warehouse assistant",
                "document_id": document_id,
            },
        )
    assert response.status_code == 422
