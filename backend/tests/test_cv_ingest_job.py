# allow-hardcode: the model responses, names and SQL below are test fixture
# content, not a matching oracle.
"""The job that resolves a CV with no candidate named to the right person.

No test here reaches a model or the network: `extract_identity` is faked, the
body store is the in-memory double, and the handoff enqueue is captured. The
questions worth asking are the ones the job owns: does a new CV create a new
candidate, does an email that matches an existing candidate attach to it, does
a colleague-held match become `needs_review` rather than a silent overwrite, and
is a placeholder left empty by a re-bind removed rather than seeded into the
list.

These run against a real tenant-scoped session — the place SQL and RLS bugs
hide when a test uses `session=None`.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models.candidate import CandidateDocument
from app.services.cv.identity import IdentityField, IdentityResult
from app.services.llm.client import LLMResult
from app.services.storage.r2 import InMemoryBodyStore
from app.workers import ingest_jobs
from tests.conftest import AdminSessionLocal
from tests.test_candidate_roles_api import agency  # noqa: F401
from tests.test_cv_text import _pdf_with_text_pages

CV_TEXT = (
    "Evelyn Tan\n"
    "evelyn.tan@example.com\n"
    "+65 9123 4567\n"
    "Senior Recruiter at KLN Logistics.\n"
)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """The job refuses to touch a row before a model is configured."""
    monkeypatch.setattr(settings, "CEREBRAS_BASE_URL", "https://cerebras.test/v1")
    monkeypatch.setattr(settings, "CEREBRAS_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EXTRACTION_MODEL_FAST", "test/fast")


@pytest.fixture
def store(monkeypatch) -> InMemoryBodyStore:
    double = InMemoryBodyStore()
    monkeypatch.setattr(ingest_jobs, "body_store", lambda: double)
    return double


@pytest.fixture
def enqueued(monkeypatch):
    """Captures the handoff enqueue; Redis is never touched."""
    jobs: list[tuple[str, dict]] = []

    async def _enqueue(name: str, **kwargs) -> bool:
        jobs.append((name, kwargs))
        return True

    monkeypatch.setattr(ingest_jobs, "enqueue", _enqueue)
    return jobs


def _identity(full_name="Evelyn Tan", email="evelyn.tan@example.com", phone="+65 9123 4567"):
    """A fake identity extractor returning fields that quote the CV."""

    def _field(value: str | None) -> IdentityField | None:
        if value is None:
            return None
        at = CV_TEXT.find(value)
        return IdentityField(
            value=value,
            evidence=value,
            start_char=at,
            end_char=at + len(value),
            confidence=0.9,
        )

    payload = {"full_name": full_name, "email": email, "phone": phone}
    result = IdentityResult(
        full_name=_field(payload["full_name"]),
        email=_field(payload["email"]),
        phone=_field(payload["phone"]),
    )

    async def _extract(text: str, **kwargs):
        return result, LLMResult(data={}, model="test/fast", latency_ms=1, raw={})

    return _extract


async def _placeholder_and_document(
    tenant_id, user_id, store, data: bytes, *, state=CandidateDocument.INGEST_PENDING
):
    """Seed a placeholder candidate and an ingest_pending document, the route's shape."""
    candidate_id = uuid.uuid4()
    document_id = uuid.uuid4()
    key = f"{tenant_id}/documents/{document_id}.pdf"
    if data is not None:
        await store.put_bytes(key, data, "application/pdf")
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, pipeline_stage, "
                "record_status, owner_id, created_by, updated_by) "
                "VALUES (:i, :t, 'Uploaded CV', 'new', 'active', :u, :u, :u)"
            ),
            {"i": candidate_id, "t": tenant_id, "u": user_id},
        )
        await s.execute(
            text(
                "INSERT INTO candidate_documents (id, tenant_id, candidate_id, filename,"
                " content_type, byte_size, object_key, parse_state, origin, uploaded_by)"
                " VALUES (:i, :t, :c, 'cv.pdf', 'application/pdf', :b, :k, :s, 'ingest', :u)"
            ),
            {
                "i": document_id,
                "t": tenant_id,
                "c": candidate_id,
                "b": len(data or b""),
                "k": key,
                "s": state,
                "u": user_id,
            },
        )
        await s.commit()
    return candidate_id, document_id


async def _document(document_id):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT parse_state, parse_error, candidate_id "
                    "FROM candidate_documents WHERE id = :i"
                ),
                {"i": document_id},
            )
        ).one()


async def _candidate(candidate_id):
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT full_name, email, phone_e164, owner_id "
                    "FROM candidates WHERE id = :i"
                ),
                {"i": candidate_id},
            )
        ).one()


async def _candidate_count(tenant_id) -> int:
    async with AdminSessionLocal() as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM candidates WHERE tenant_id = :t"), {"t": tenant_id}
            )
        ).scalar_one()


async def _cleanup(tenant_id):
    async with AdminSessionLocal() as s:
        for table in (
            "extraction_evidence",
            "extractions",
            "candidate_documents",
            "candidate_roles",
            "candidate_field_overrides",
            "candidate_skills",
            "candidates",
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        await s.commit()


@pytest.mark.asyncio
async def test_a_new_cv_creates_a_candidate_and_hands_off_to_parse(
    agency, store, enqueued, monkeypatch
):  # noqa: F811
    tenant_id, user_id = agency
    placeholder_id, document_id = await _placeholder_and_document(
        tenant_id, user_id, store, _pdf_with_text_pages(1, CV_TEXT)
    )

    monkeypatch.setattr(ingest_jobs, "extract_identity", _identity())
    await ingest_jobs.ingest_candidate_cv(
        None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    row = await _document(document_id)
    # Handed off to the parse job as an ordinary pending document.
    assert row.parse_state == CandidateDocument.PENDING
    assert row.parse_error is None

    # The placeholder was replaced — the document now points at the new
    # candidate, and the empty placeholder was removed.
    assert row.candidate_id != placeholder_id
    assert await _candidate_count(tenant_id) == 1
    created = await _candidate(row.candidate_id)
    assert created.full_name == "Evelyn Tan"
    assert created.email == "evelyn.tan@example.com"
    assert created.owner_id == user_id

    assert len(enqueued) == 1
    name, kwargs = enqueued[0]
    assert name == "parse_candidate_cv"
    assert kwargs["candidate_id"] == str(row.candidate_id)
    assert kwargs["document_id"] == str(document_id)
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_cv_matching_an_existing_candidate_attaches_to_it(
    agency, store, enqueued, monkeypatch
):  # noqa: F811
    """Identity resolution reuses the person already in the database."""
    tenant_id, user_id = agency
    # An existing candidate the new CV's email will match.
    existing_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, email, pipeline_stage, "
                "record_status, owner_id, created_by, updated_by) "
                "VALUES (:i, :t, 'Eve T', :e, 'new', 'active', :u, :u, :u)"
            ),
            {"i": existing_id, "t": tenant_id, "e": "evelyn.tan@example.com", "u": user_id},
        )
        await s.commit()

    placeholder_id, document_id = await _placeholder_and_document(
        tenant_id, user_id, store, _pdf_with_text_pages(1, CV_TEXT)
    )

    monkeypatch.setattr(ingest_jobs, "extract_identity", _identity())
    await ingest_jobs.ingest_candidate_cv(
        None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    row = await _document(document_id)
    assert row.parse_state == CandidateDocument.PENDING
    assert row.candidate_id == existing_id
    # Placeholder removed; only the existing candidate remains.
    assert await _candidate_count(tenant_id) == 1
    # The existing candidate's name is NOT overwritten by the CV's fuller one —
    # a match attaches the document, it does not edit the record.
    existing = await _candidate(existing_id)
    assert existing.full_name == "Eve T"
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_cv_with_no_contact_details_creates_a_candidate_with_no_email(
    agency, store, enqueued, monkeypatch
):  # noqa: F811
    """A CV that states no email and no phone still becomes a candidate.

    Identity is email-or-phone; without either, the candidate has no matchable
    key, but the CV still has a career worth parsing. The candidate is created
    with the name the model read (or a placeholder) and parse runs anyway.
    """
    tenant_id, user_id = agency
    placeholder_id, document_id = await _placeholder_and_document(
        tenant_id, user_id, store, _pdf_with_text_pages(1, CV_TEXT)
    )

    monkeypatch.setattr(
        ingest_jobs, "extract_identity", _identity(email=None, phone=None)
    )
    await ingest_jobs.ingest_candidate_cv(
        None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    row = await _document(document_id)
    assert row.parse_state == CandidateDocument.PENDING
    assert row.candidate_id != placeholder_id
    created = await _candidate(row.candidate_id)
    assert created.email is None
    assert created.phone_e164 is None
    assert created.full_name == "Evelyn Tan"
    assert len(enqueued) == 1
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_a_colleague_held_match_becomes_needs_review(
    agency, store, enqueued, monkeypatch
):  # noqa: F811
    """A match the uploader cannot see is flagged, never silently attached.

    An owner sees every candidate in the agency (`visible_candidates` returns
    `true_()`), so the invisible-match case only arises for a recruiter whose
    colleague holds the candidate privately. The uploader here is a recruiter,
    and the matching candidate is owned by a different recruiter.
    """
    tenant_id, _owner_id = agency
    # A recruiter uploader — the role the job reads off the user row.
    uploader_id = uuid.uuid4()
    holder_id = uuid.uuid4()
    existing_id = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'recruiter')"
            ),
            {"i": uploader_id, "t": tenant_id, "e": f"up{uploader_id.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'recruiter')"
            ),
            {"i": holder_id, "t": tenant_id, "e": f"holder{holder_id.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, email, pipeline_stage, "
                "record_status, owner_id, created_by, updated_by) "
                "VALUES (:i, :t, 'Eve T', :e, 'new', 'active', :h, :h, :h)"
            ),
            {"i": existing_id, "t": tenant_id, "e": "evelyn.tan@example.com", "h": holder_id},
        )
        await s.commit()

    placeholder_id, document_id = await _placeholder_and_document(
        tenant_id, uploader_id, store, _pdf_with_text_pages(1, CV_TEXT)
    )

    monkeypatch.setattr(ingest_jobs, "extract_identity", _identity())
    await ingest_jobs.ingest_candidate_cv(
        None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    row = await _document(document_id)
    assert row.parse_state == CandidateDocument.NEEDS_REVIEW
    assert row.parse_error is not None
    # The parse is NOT enqueued while the candidate is in dispute.
    assert enqueued == []
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_an_unreadable_file_is_terminal(agency, store, enqueued):
    # noqa: F811
    tenant_id, user_id = agency
    placeholder_id, document_id = await _placeholder_and_document(
        tenant_id, user_id, store, b"this is not a PDF at all"
    )

    await ingest_jobs.ingest_candidate_cv(
        None, tenant_id=str(tenant_id), document_id=str(document_id)
    )

    row = await _document(document_id)
    assert row.parse_state == CandidateDocument.UNREADABLE
    assert row.parse_error
    assert enqueued == []
    await _cleanup(tenant_id)


@pytest.mark.asyncio
async def test_replaying_the_job_on_a_resolved_document_does_nothing(
    agency, store, enqueued, monkeypatch
):  # noqa: F811
    tenant_id, user_id = agency
    # A document that already resolved sits at `pending` (handed off to parse);
    # replaying the ingest job on one must be a no-op, not a second resolution.
    placeholder_id, document_id = await _placeholder_and_document(
        tenant_id, user_id, store, _pdf_with_text_pages(1, CV_TEXT), state="pending"
    )

    async def _never(text: str, **kwargs):
        raise AssertionError("a resolved document must not be read again")

    monkeypatch.setattr(ingest_jobs, "extract_identity", _never)
    await ingest_jobs.ingest_candidate_cv(
        None, tenant_id=str(tenant_id), document_id=str(document_id)
    )
    # The job returned without raising and without enqueuing.
    assert enqueued == []
    await _cleanup(tenant_id)
