"""A candidate's CV: upload it, fetch it back, remove it.

Its own module rather than more of `candidates.py`, which is already close to
the size ceiling — and, as with `candidates_avatar.py`, none of this is really
about candidate *records*. It is about bytes that arrive from a browser and
must be treated as hostile until proven otherwise.

The rules that shape it are the avatar module's, with one addition:

1. **The key is computed, never received.** `document_key` is built from the
   tenant on the session cookie and an id we mint, so a filename containing
   `../` cannot escape the `{tenant_id}/` prefix a tenant erasure purges by.
2. **The candidate is resolved through the tenant session**, so another
   agency's id is a 404 rather than a 403.
3. **Nothing the client says about the file is believed.** Not the filename,
   not the Content-Type — `sniff` reads the bytes. Unlike an avatar the file
   is *not* re-encoded, because an evidence span recorded later is an offset
   into text extracted from exactly these bytes.
4. **An upload costs money.** A parse is a model call, so a tenant's uploads
   are capped per day. See `_within_daily_quota`.
"""

import uuid
from datetime import UTC, datetime, time
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy import func, select

from app.api.auth import _require_session_with_role
from app.api.candidates import _load
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateDocument
from app.services.cv.convert import is_legacy_office
from app.services.cv.text import sniff
from app.services.storage.r2 import BodyStore, R2BodyStore, document_key
from app.services.visibility import (
    load_editable_candidate,
    load_visible_candidate,
)
from app.workers.queue import enqueue

log = get_logger(__name__)

router = APIRouter(tags=["candidates"])

# What each sniffed kind really is, so R2 serves the download under a
# Content-Type that matches the bytes rather than under whatever the browser
# guessed from the file extension.
_MIME_FOR_KIND = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    # Legacy Word: stored as-is, converted to .docx inside the worker.
    "doc": "application/msword",
}

# Postgres would raise on a longer value anyway; truncating here means an
# absurd filename is a stored oddity rather than a 500.
_MAX_FILENAME = 255

_ENQUEUE_FAILED = (
    "This CV was saved but could not be queued for reading. Try again in a "
    "few minutes."
)


def body_store() -> BodyStore:
    """The object store, as a dependency so tests can substitute the double.

    A FastAPI dependency rather than a module global, for the reason
    `candidates_avatar.body_store` gives: `app.dependency_overrides` is scoped
    to the test that sets it, whereas a patched global outlives a failing test.
    """
    return R2BodyStore()


def serialize(document: CandidateDocument) -> dict:
    """What the panel needs to describe one upload, and nothing more.

    `object_key` and `text_key` are deliberately absent: they are internal
    storage addresses, and a client that knew them would be one presigning bug
    away from naming an object itself.
    """
    return {
        "id": str(document.id),
        "filename": document.filename,
        "content_type": document.content_type,
        "byte_size": document.byte_size,
        "parse_state": document.parse_state,
        "parse_error": document.parse_error,
        "text_chars": document.text_chars,
        # Both stay set on a `parsed` document — this is a note about a
        # success, not an error. See `CandidateDocument`.
        "dropped_count": document.dropped_count,
        "dropped_reason": document.dropped_reason,
        "created_at": document.created_at.isoformat() if document.created_at else None,
    }


async def documents_for(session, candidate_id: uuid.UUID) -> list[CandidateDocument]:
    """Newest first — the CV a recruiter just uploaded is the one they want."""
    return list(
        (
            await session.execute(
                select(CandidateDocument)
                .where(CandidateDocument.candidate_id == candidate_id)
                .order_by(CandidateDocument.created_at.desc())
            )
        ).scalars()
    )


async def _within_daily_quota(session) -> bool:
    """Has this tenant any uploads left today?

    There is no counter anywhere. This is a `COUNT(*)` of the tenant's own
    `candidate_documents` rows created since midnight UTC, run inside the
    tenant-scoped session so RLS does the scoping — a counter table would be a
    second number that can disagree with the documents it counts, which is a
    worse problem than either consequence below.

    Two consequences are accepted deliberately:

    * **Deleting a document frees quota.** A hole only somebody deliberately
      gaming their own bill would use.
    * **A document that failed to enqueue still consumed one.** The safer
      direction to err — the row exists and `rescan_stuck` may yet parse it.

    The count is also taken before the body is read, so two simultaneous
    uploads on the last slot can both pass. Being one over on a daily cap is
    not worth a lock held across a multi-megabyte read.
    """
    since = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    used = (
        await session.execute(
            select(func.count())
            .select_from(CandidateDocument)
            .where(CandidateDocument.created_at >= since)
        )
    ).scalar_one()
    return used < settings.CV_DAILY_PARSE_QUOTA


async def _read_within_limit(upload: UploadFile) -> bytes:
    """Read the upload, refusing anything over the configured size.

    Counted here as the bytes arrive rather than taken from `Content-Length`,
    which is the client's claim and which a streaming upload need not send at
    all. Reading one byte past the limit and stopping means a 10 GB post costs
    the limit plus a byte, not 10 GB.
    """
    limit = settings.CV_MAX_UPLOAD_BYTES
    content = await upload.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"This file is larger than the {limit} byte limit.",
        )
    if not content:
        raise HTTPException(status_code=400, detail="No file was uploaded.")
    return content


def _safe_filename(raw: str | None, kind: str) -> str:
    """The client's name for the file, kept only for display.

    Stripped to its last path segment because a browser on some platforms
    sends a full path, and because the value is shown back to a recruiter. It
    never reaches the object key — `document_key` is computed — so this is
    presentation hygiene, not a security boundary.
    """
    name = PurePosixPath((raw or "").replace("\\", "/")).name.strip()
    return name[:_MAX_FILENAME] if name else f"cv.{kind}"


@router.post("/candidates/{candidate_id}/documents", status_code=202)
async def upload_document(
    request: Request,
    candidate_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """Accept a CV and queue it for reading.

    202, not 201: the row exists but the answer does not. Everything that can
    refuse the request — the tenant check and the quota — happens before a
    byte is read, so a caller guessing another agency's candidate id cannot
    even spend our bandwidth, and an exhausted tenant is refused with nothing
    stored anywhere.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        await load_editable_candidate(session, candidate_id, user_uuid, role)
        if not await _within_daily_quota(session):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"This agency has uploaded its {settings.CV_DAILY_PARSE_QUOTA} "
                    "CVs for today. The limit resets at midnight UTC."
                ),
            )

    content = await _read_within_limit(file)

    # The bytes decide, not the extension and not the Content-Type. 415 rather
    # than 400: the request was well formed, the media type is what we refuse.
    kind = sniff(content)
    if kind is None and is_legacy_office(content):
        # A .doc (Word 97-2003) is stored as-is and converted to .docx inside the
        # worker, where LibreOffice runs. Accepted here so the job sees it; the
        # route never shells out itself.
        kind = "doc"
    if kind is None:
        raise HTTPException(
            status_code=415,
            detail="Only PDF and Word (.docx) files can be read, whatever this was named.",
        )

    document_id = uuid.uuid4()
    # Derived from the authenticated tenant and an id we mint, never from
    # anything the client sent. This is the line that makes cross-tenant
    # writes impossible.
    key = document_key(tenant_uuid, candidate_id, document_id, kind)
    await store.put_bytes(key, content, _MIME_FOR_KIND[kind])

    async with tenant_session(tenant_uuid) as session:
        # Re-checked inside the transaction that writes: the candidate could
        # have been deleted while the upload was in flight, and the INSERT is
        # tenant-scoped by RLS regardless.
        await _load(session, candidate_id)
        document = CandidateDocument(
            id=document_id,
            tenant_id=tenant_uuid,
            candidate_id=candidate_id,
            filename=_safe_filename(file.filename, kind),
            content_type=_MIME_FOR_KIND[kind],
            byte_size=len(content),
            object_key=key,
            parse_state=CandidateDocument.PENDING,
            uploaded_by=user_uuid,
        )
        session.add(document)
        await session.commit()

    # Enqueued after the commit, because the job reads the row it is named
    # for. `enqueue` returns a bool and never raises (`queue.py`), so a silent
    # Redis outage would otherwise leave this document `pending` until
    # `rescan_stuck` happened by — which is a sweep, not a promise. Say so on
    # the row instead: `failed` is the retryable terminal state, so the panel
    # offers a retry rather than a spinner nobody is behind.
    if not await enqueue(
        "parse_candidate_cv",
        tenant_id=str(tenant_uuid),
        candidate_id=str(candidate_id),
        document_id=str(document_id),
    ):
        log.warning("cv_upload_enqueue_failed", candidate_document_id=str(document_id))
        async with tenant_session(tenant_uuid) as session:
            document = await session.get(CandidateDocument, document_id)
            if document is not None:
                document.parse_state = CandidateDocument.FAILED
                document.parse_error = _ENQUEUE_FAILED
                await session.commit()
                return serialize(document)

    async with tenant_session(tenant_uuid) as session:
        stored = await session.get(CandidateDocument, document_id)
        if stored is None:  # pragma: no cover - deleted between two statements
            raise HTTPException(status_code=404, detail="Document not found")
        return serialize(stored)


@router.post("/candidates/documents", status_code=202)
async def upload_document_no_candidate(
    request: Request,
    file: Annotated[UploadFile, File()],
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """Accept a CV and resolve its own candidate.

    The sibling `upload_document` route requires a candidate id, because the
    recruiter already knew who the CV belonged to. This one is the drop-a-CV-in
    path: the platform reads the document's contact details and matches them to
    an existing person or creates a new one. The bytes are accepted the same
    hostile-until-proven way — computed key, sniffed type, daily quota checked
    before a byte is read — and the document is parked at `ingest_pending`
    against a placeholder candidate, because the foreign key is NOT NULL and
    identity has not been read yet. The ingest job re-binds the document to the
    resolved candidate (and deletes the placeholder when it is left empty).

    Declared before `/candidates/{candidate_id}/documents` would be enough on
    its own (different path depth), but the router is included before
    `candidates` in `main.py` so the LITERAL `documents` segment is never
    swallowed by `/candidates/{candidate_id}`.
    """
    user_uuid, tenant_uuid, _ = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        if not await _within_daily_quota(session):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"This agency has uploaded its {settings.CV_DAILY_PARSE_QUOTA} "
                    "CVs for today. The limit resets at midnight UTC."
                ),
            )

    content = await _read_within_limit(file)

    kind = sniff(content)
    if kind is None and is_legacy_office(content):
        kind = "doc"
    if kind is None:
        raise HTTPException(
            status_code=415,
            detail="Only PDF and Word (.docx) files can be read, whatever this was named.",
        )

    # A placeholder candidate holds the NOT NULL foreign key until the ingest
    # job reads identity and re-binds the document. It carries no contact
    # details and the default `new` stage — the exact shape `_delete_if_ghost`
    # looks for, so a placeholder left empty by a successful re-bind is removed
    # rather than seeded into the candidate list. Created up front rather than
    # in the job so this route's commit is self-contained: a document row never
    # exists without its candidate.
    document_id = uuid.uuid4()
    async with tenant_session(tenant_uuid) as session:
        placeholder = Candidate(
            tenant_id=tenant_uuid,
            full_name=_safe_filename(file.filename, kind)[:1000] or "Uploaded CV",
            pipeline_stage=Candidate.STAGES[0],
            record_status=Candidate.ACTIVE,
            owner_id=user_uuid,
            created_by=user_uuid,
            updated_by=user_uuid,
        )
        session.add(placeholder)
        await session.flush()
        candidate_id = placeholder.id

        key = document_key(tenant_uuid, candidate_id, document_id, kind)
        await store.put_bytes(key, content, _MIME_FOR_KIND[kind])
        document = CandidateDocument(
            id=document_id,
            tenant_id=tenant_uuid,
            candidate_id=candidate_id,
            filename=_safe_filename(file.filename, kind),
            content_type=_MIME_FOR_KIND[kind],
            byte_size=len(content),
            object_key=key,
            parse_state=CandidateDocument.INGEST_PENDING,
            origin=CandidateDocument.INGEST,
            uploaded_by=user_uuid,
        )
        session.add(document)
        await session.commit()

    if not await enqueue(
        "ingest_candidate_cv",
        tenant_id=str(tenant_uuid),
        document_id=str(document_id),
    ):
        log.warning("cv_ingest_enqueue_failed", candidate_document_id=str(document_id))
        async with tenant_session(tenant_uuid) as session:
            document = await session.get(CandidateDocument, document_id)
            if document is not None:
                document.parse_state = CandidateDocument.FAILED
                document.parse_error = _ENQUEUE_FAILED
                await session.commit()
                return serialize(document)

    async with tenant_session(tenant_uuid) as session:
        stored = await session.get(CandidateDocument, document_id)
        if stored is None:  # pragma: no cover - deleted between two statements
            raise HTTPException(status_code=404, detail="Document not found")
        return serialize(stored)


async def _load_document(
    session, candidate_id: uuid.UUID, document_id: uuid.UUID
) -> CandidateDocument:
    """Scoped by the candidate as well as the document's own id.

    Same reason `_load_role` is: a document id alone would let one candidate's
    file be reached through another's URL — inside the tenant, but still a
    record nobody asked for.
    """
    document = (
        await session.execute(
            select(CandidateDocument).where(
                CandidateDocument.id == document_id,
                CandidateDocument.candidate_id == candidate_id,
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.get("/candidates/{candidate_id}/documents/{document_id}/download")
async def download_document(
    request: Request,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """A short-lived URL the browser can fetch the original file from.

    Signed per request, after the tenant check, and never stored: a persisted
    URL would be a capability that outlives the permission it was granted
    under.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        await load_visible_candidate(session, candidate_id, user_uuid, role)
        document = await _load_document(session, candidate_id, document_id)
        key = document.object_key

    # The key was computed by us at upload and is not client-writable, but the
    # prefix is checked before signing anyway: without this, any future write
    # path that got a value into `object_key` would become a way to sign an
    # arbitrary object in the bucket, including another tenant's.
    if not key.startswith(f"{tenant_uuid}/"):  # pragma: no cover - defence in depth
        raise HTTPException(status_code=404, detail="Document not found")

    ttl = settings.CV_PRESIGNED_URL_TTL_SECONDS
    return {"url": await store.presigned_get(key, ttl), "expires_in": ttl}


@router.delete("/candidates/{candidate_id}/documents/{document_id}", status_code=204)
async def delete_document(
    request: Request,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    store: Annotated[BodyStore, Depends(body_store)],
) -> Response:
    """Remove the file: the objects first, then the row.

    That order is deliberate, as with an avatar. If the object delete fails
    the row still names it, so a retry can find it; the reverse leaves bytes
    in R2 that nothing references and no sweep can locate.

    The roles and skills this document produced are left exactly where they
    are. They are the recruiter's record now — deleting the PDF is a storage
    decision, and it must not silently retract a career history somebody has
    already reviewed. What does go is the extraction rows, by the cascade on
    `extractions.candidate_document_id`, since evidence spans index into text
    that no longer exists.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        await load_editable_candidate(session, candidate_id, user_uuid, role)
        document = await _load_document(session, candidate_id, document_id)
        keys = [document.object_key]
        if document.text_key:
            keys.append(document.text_key)

    await store.delete(*keys)

    async with tenant_session(tenant_uuid) as session:
        document = await session.get(CandidateDocument, document_id)
        # Gone already — a double-clicked delete is not an error, and the row
        # was tenant-checked a moment ago in the transaction above.
        if document is not None:
            await session.delete(document)
            await session.commit()

    return Response(status_code=204)
