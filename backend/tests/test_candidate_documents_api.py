# allow-hardcode: "Parkway Shenton" / "Staff Nurse" / "Jane Tan" below are
# test fixture content, not a matching oracle.
"""Uploading a CV, fetching it back, deleting it, and answering for it.

Every test here is a boundary rather than a feature. The upload path takes
bytes from a browser, spends an agency's model budget on them and writes them
to shared object storage, so the questions worth asking are adversarial: can a
caller reach another agency's candidate, can they hand us a PNG named `.pdf`,
and can they spend a day's quota twice.
"""

import io
import uuid
import zipfile
from datetime import date

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


async def _seed_agency() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid, uid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, pipeline_stage, "
                "record_status) VALUES (:i, :t, 'Jane Tan', 'new', 'active')"
            ),
            {"i": cid, "t": tid},
        )
        await s.commit()
    return tid, uid, cid


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
    tid, uid, cid = await _seed_agency()
    yield tid, uid, cid
    await _drop_agency(tid)


@pytest.fixture
async def other_agency():
    """A second agency, so "not yours" can be told apart from "not there"."""
    tid, uid, cid = await _seed_agency()
    yield tid, uid, cid
    await _drop_agency(tid)


@pytest.fixture
async def store():
    """The object store, swapped for the in-memory double.

    Through `app.dependency_overrides` rather than a patched global: the
    override is undone even when a test fails, so a failure cannot leave the
    next test writing to real R2.
    """
    double = InMemoryBodyStore()
    app.dependency_overrides[candidate_documents.body_store] = lambda: double
    yield double
    app.dependency_overrides.pop(candidate_documents.body_store, None)


@pytest.fixture
async def queued(monkeypatch):
    """Every job the upload route tried to enqueue. Redis is never touched."""
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


async def _upload(http: AsyncClient, cid: uuid.UUID, content: bytes, **kwargs):
    return await http.post(
        f"/api/candidates/{cid}/documents",
        files={
            "file": (
                kwargs.get("name", "cv.pdf"),
                content,
                kwargs.get("type", "application/pdf"),
            )
        },
    )


async def _document_rows(tid: uuid.UUID) -> list:
    async with AdminSessionLocal() as s:
        rows = await s.execute(
            text("SELECT id, parse_state FROM candidate_documents WHERE tenant_id = :t"),
            {"t": tid},
        )
        return list(rows)


# --- Upload -----------------------------------------------------------------


async def test_upload_stores_enqueues_and_returns_202(agency, store, queued):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, _pdf_bytes())

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["parse_state"] == CandidateDocument.PENDING
    assert body["filename"] == "cv.pdf"

    # The acceptance criterion this whole route exists for: without the
    # enqueue an uploaded CV waits for whenever `rescan_stuck` next runs.
    assert len(queued) == 1
    name, kwargs = queued[0]
    assert name == "parse_candidate_cv"
    assert kwargs == {
        "tenant_id": str(tid),
        "candidate_id": str(cid),
        "document_id": body["id"],
    }
    assert store.binary_objects  # the bytes really were written


async def test_upload_accepts_a_docx(agency, store, queued):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, _docx_bytes(), name="cv.docx")
    assert response.status_code == 202, response.text


async def test_a_failed_enqueue_lands_the_row_in_failed(agency, store, monkeypatch):
    """`enqueue` returns False on a Redis outage and never raises.

    Left at `pending` the document would look queued forever to a recruiter
    watching the panel, so the row has to say out loud that nobody is coming.
    """
    tid, uid, cid = agency

    async def _refuse(name: str, **kwargs) -> bool:
        return False

    monkeypatch.setattr(candidate_documents, "enqueue", _refuse)

    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, _pdf_bytes())

    assert response.status_code == 202, response.text
    assert response.json()["parse_state"] == CandidateDocument.FAILED
    assert "again" in response.json()["parse_error"].lower()
    assert [state for _id, state in await _document_rows(tid)] == [CandidateDocument.FAILED]


async def test_upload_to_another_agencys_candidate_is_404(agency, other_agency, store, queued):
    tid, uid, _cid = agency
    _other_tid, _other_uid, other_cid = other_agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, other_cid, _pdf_bytes())
    assert response.status_code == 404
    assert queued == []
    assert await _document_rows(tid) == []


async def test_oversized_upload_is_413(agency, store, queued, monkeypatch):
    tid, uid, cid = agency
    monkeypatch.setattr(settings, "CV_MAX_UPLOAD_BYTES", 512)
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, _pdf_bytes(padding=2048))
    assert response.status_code == 413
    assert queued == []
    assert await _document_rows(tid) == []


async def test_a_png_named_pdf_is_415(agency, store, queued):
    """The filename and the Content-Type are the client's claims, not facts."""
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, _png_bytes())
    assert response.status_code == 415
    assert queued == []
    assert await _document_rows(tid) == []


async def test_past_the_daily_quota_is_429_with_nothing_stored(
    agency, store, queued, monkeypatch
):
    tid, uid, cid = agency
    monkeypatch.setattr(settings, "CV_DAILY_PARSE_QUOTA", 1)

    async with _client_for(tid, uid) as http:
        first = await _upload(http, cid, _pdf_bytes())
        assert first.status_code == 202, first.text
        stored_after_first = dict(store.binary_objects)

        second = await _upload(http, cid, _pdf_bytes())

    assert second.status_code == 429
    # "Nothing stored" is the whole point: no second row, no second object,
    # and no second job to bill a model call for.
    assert len(await _document_rows(tid)) == 1
    assert dict(store.binary_objects) == stored_after_first
    assert len(queued) == 1


async def test_the_quota_is_per_tenant(agency, other_agency, store, queued, monkeypatch):
    """One agency exhausting its day must not close the door on another."""
    tid, uid, cid = agency
    other_tid, other_uid, other_cid = other_agency
    monkeypatch.setattr(settings, "CV_DAILY_PARSE_QUOTA", 1)

    async with _client_for(tid, uid) as http:
        assert (await _upload(http, cid, _pdf_bytes())).status_code == 202
        assert (await _upload(http, cid, _pdf_bytes())).status_code == 429

    async with _client_for(other_tid, other_uid) as http:
        assert (await _upload(http, other_cid, _pdf_bytes())).status_code == 202


# --- Download ---------------------------------------------------------------


async def test_download_returns_a_signed_url(agency, store, queued):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        doc_id = (await _upload(http, cid, _pdf_bytes())).json()["id"]
        response = await http.get(f"/api/candidates/{cid}/documents/{doc_id}/download")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["url"]
    assert body["expires_in"] == settings.CV_PRESIGNED_URL_TTL_SECONDS


async def test_download_of_another_agencys_document_is_404(agency, other_agency, store, queued):
    tid, uid, _cid = agency
    other_tid, other_uid, other_cid = other_agency

    async with _client_for(other_tid, other_uid) as http:
        doc_id = (await _upload(http, other_cid, _pdf_bytes())).json()["id"]

    async with _client_for(tid, uid) as http:
        response = await http.get(f"/api/candidates/{other_cid}/documents/{doc_id}/download")
    assert response.status_code == 404


# --- Delete -----------------------------------------------------------------


async def test_delete_removes_the_row_and_the_object(agency, store, queued):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        doc_id = (await _upload(http, cid, _pdf_bytes())).json()["id"]
        response = await http.delete(f"/api/candidates/{cid}/documents/{doc_id}")

    assert response.status_code == 204
    assert await _document_rows(tid) == []
    assert store.binary_objects == {}


async def test_delete_of_another_agencys_document_is_404(agency, other_agency, store, queued):
    tid, uid, _cid = agency
    other_tid, other_uid, other_cid = other_agency

    async with _client_for(other_tid, other_uid) as http:
        doc_id = (await _upload(http, other_cid, _pdf_bytes())).json()["id"]

    async with _client_for(tid, uid) as http:
        response = await http.delete(f"/api/candidates/{other_cid}/documents/{doc_id}")
    assert response.status_code == 404
    assert len(await _document_rows(other_tid)) == 1


async def test_deleting_a_document_leaves_the_roles_it_produced(agency, store, queued):
    """A parse's findings are the recruiter's record now, not the file's.

    Deleting the PDF is a storage decision; it must not silently retract the
    career history somebody has already reviewed.
    """
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        doc_id = (await _upload(http, cid, _pdf_bytes())).json()["id"]
        role = await http.post(
            f"/api/candidates/{cid}/roles",
            json={"employer": "Parkway Shenton", "title": "Staff Nurse"},
        )
        assert role.status_code == 201, role.text

        assert (await http.delete(f"/api/candidates/{cid}/documents/{doc_id}")).status_code == 204

        remaining = await http.get(f"/api/candidates/{cid}")
    assert [r["employer"] for r in remaining.json()["roles"]] == ["Parkway Shenton"]


# --- Reprocess (re-queue without re-upload) ----------------------------------


async def test_reprocess_resets_a_failed_document_and_enqueues(agency, store, queued):
    """A failed document can be re-queued by id — the bytes are already stored,
    and the recruiter should not need to choose the file again."""
    tid, uid, cid = agency

    async with _client_for(tid, uid) as http:
        doc = (await _upload(http, cid, _pdf_bytes())).json()

    # Simulate a failed parse (the worker sets this on a crash/timeout).
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "UPDATE candidate_documents SET parse_state = 'failed',"
                " attempts = 3, parse_error = 'model failed' WHERE id = :i"
            ),
            {"i": doc["id"]},
        )
        await s.commit()

    # Clear the upload enqueue so the reprocess enqueue is distinguishable.
    queued.clear()

    async with _client_for(tid, uid) as http:
        response = await http.post(
            f"/api/candidates/{cid}/documents/{doc['id']}/reprocess"
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["id"] == doc["id"]
    assert body["parse_state"] == CandidateDocument.PENDING

    # The reprocess enqueued a parse job, not uploaded a new document.
    assert len(queued) == 1
    name, kwargs = queued[0]
    assert name == "parse_candidate_cv"
    assert kwargs == {
        "tenant_id": str(tid),
        "candidate_id": str(cid),
        "document_id": doc["id"],
    }
    # The attempts counter was zeroed so the job's conditional claim works.
    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text("SELECT attempts FROM candidate_documents WHERE id = :i"),
                {"i": doc["id"]},
            )
        ).scalar_one()
    assert row == 0


async def test_reprocess_of_another_agencys_document_is_404(
    agency, other_agency, store, queued
):
    """Cross-tenant reprocess is refused, exactly like delete."""
    tid, uid, _cid = agency
    other_tid, other_uid, other_cid = other_agency

    async with _client_for(other_tid, other_uid) as http:
        doc = (await _upload(http, other_cid, _pdf_bytes())).json()

    async with _client_for(tid, uid) as http:
        response = await http.post(
            f"/api/candidates/{other_cid}/documents/{doc['id']}/reprocess"
        )
    assert response.status_code == 404


async def test_reprocess_fails_gracefully_when_enqueue_loses(
    agency, store, monkeypatch
):
    """When Redis is down, the reprocess lands the row as failed rather than
    sitting at `pending` with nobody coming to read it."""
    tid, uid, cid = agency

    async with _client_for(tid, uid) as http:
        doc = (await _upload(http, cid, _pdf_bytes())).json()

    async def _refuse(name: str, **kwargs) -> bool:
        return False

    monkeypatch.setattr(candidate_documents, "enqueue", _refuse)

    async with _client_for(tid, uid) as http:
        response = await http.post(
            f"/api/candidates/{cid}/documents/{doc['id']}/reprocess"
        )

    assert response.status_code == 202, response.text
    assert response.json()["parse_state"] == CandidateDocument.FAILED


# --- The candidate GET ------------------------------------------------------


async def test_documents_are_embedded_in_the_candidate(agency, store, queued):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        doc_id = (await _upload(http, cid, _pdf_bytes())).json()["id"]
        candidate = await http.get(f"/api/candidates/{cid}")

    documents = candidate.json()["documents"]
    assert [d["id"] for d in documents] == [doc_id]
    assert documents[0]["parse_state"] == CandidateDocument.PENDING


# --- Confirm and reject -----------------------------------------------------


async def _unconfirmed_role(tid: uuid.UUID, cid: uuid.UUID, **values) -> uuid.UUID:
    """A role as a parse leaves it: attributed to the CV, awaiting a person."""
    role_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidate_roles (id, tenant_id, candidate_id, employer, "
                "employer_normalized, title, title_normalized, started_on, ended_on, "
                "source, status) VALUES (:i, :t, :c, :e, :en, :ti, :tn, :s, :en_d, "
                "'cv_upload', 'unconfirmed')"
            ),
            {
                "i": role_id,
                "t": tid,
                "c": cid,
                "e": values["employer"],
                "en": values["employer"].lower(),
                "ti": values["title"],
                "tn": values["title"].lower(),
                "s": values.get("started_on"),
                "en_d": values.get("ended_on"),
            },
        )
        await s.commit()
    return role_id


async def test_confirming_a_role_updates_the_derived_employer(agency):
    tid, uid, cid = agency
    role_id = await _unconfirmed_role(
        tid, cid, employer="Parkway Shenton", title="Staff Nurse", started_on=date(2022, 1, 1)
    )

    async with _client_for(tid, uid) as http:
        response = await http.post(f"/api/candidates/{cid}/roles/{role_id}/confirm")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "confirmed"

        candidate = await http.get(f"/api/candidates/{cid}")
    assert candidate.json()["current_employer"] == "Parkway Shenton"


async def test_rejecting_a_role_excludes_it_from_derivation(agency):
    tid, uid, cid = agency
    role_id = await _unconfirmed_role(
        tid, cid, employer="Parkway Shenton", title="Staff Nurse", started_on=date(2022, 1, 1)
    )

    async with _client_for(tid, uid) as http:
        confirmed = await http.post(f"/api/candidates/{cid}/roles/{role_id}/confirm")
        assert confirmed.status_code == 200
        rejected = await http.post(f"/api/candidates/{cid}/roles/{role_id}/reject")
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "rejected"

        candidate = await http.get(f"/api/candidates/{cid}")
    # Nothing but a rejected role is left, so there is no source for an
    # employer — and §15 says we must not keep asserting one.
    assert candidate.json()["current_employer"] is None


async def test_confirming_another_agencys_role_is_404(agency, other_agency):
    tid, uid, _cid = agency
    other_tid, _other_uid, other_cid = other_agency
    role_id = await _unconfirmed_role(
        other_tid, other_cid, employer="Parkway Shenton", title="Staff Nurse"
    )
    async with _client_for(tid, uid) as http:
        response = await http.post(f"/api/candidates/{other_cid}/roles/{role_id}/confirm")
    assert response.status_code == 404
