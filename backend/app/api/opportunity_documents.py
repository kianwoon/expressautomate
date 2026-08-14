"""Job-description files for the New job order dialog: upload, poll, download.

Its own module rather than more of `opportunities.py`, which is at the repo's
size ceiling — and, as with `candidate_documents.py`, none of this is really
about job-order *records*. It is about bytes that arrive from a browser and
must be treated as hostile until proven otherwise.

The rules are the avatar/CV modules' with one difference: **the row exists
before the opportunity does.** The create-dialog flow is upload → extract →
review → save → link, so this router owns the no-opportunity-yet upload path
(`POST /opportunities/documents`) as its first route, and a literal `documents`
segment must never be swallowed by `/opportunities/{opportunity_id}` — hence
declared before `opportunities.router` in `main.py`, the same convention as
`candidate_documents` vs `candidates`.

1. **The key is computed, never received.** `opportunity_document_key` is built
   from the tenant on the session cookie and ids we mint, so a filename
   containing `../` cannot escape the `{tenant_id}/` prefix a tenant erasure
   purges by.
2. **The bytes decide, not the filename or Content-Type.** `sniff` reads the
   bytes; a PNG named `.pdf` is 415.
3. **An upload buys a model call.** Uploads are capped per agency per day
   (`OPPORTUNITY_DOCUMENT_DAILY_QUOTA`, the same COUNT(*)-since-midnight
   shape as the CV quota) — this is a create-dialog action, one file per
   vacancy, and the extraction job is registered with its own arq timeout,
   with `rescan_stuck` recovering a stranded row.
"""

import uuid
from datetime import UTC, datetime, time
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy import func, select

from app.api.auth import _require_session_with_role
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models import OpportunityDocument
from app.services.storage.r2 import (
    BodyStore,
    R2BodyStore,
    opportunity_document_key,
)
from app.services.visibility import load_editable_opportunity, load_visible_opportunity
from app.workers.queue import enqueue

log = get_logger(__name__)

router = APIRouter(tags=["opportunities"])

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
    "This file was saved but could not be queued for reading. Try again in a "
    "few minutes."
)


def body_store() -> BodyStore:
    """The object store, as a dependency so tests can substitute the double.

    A FastAPI dependency rather than a module global, for the reason
    `candidate_documents.body_store` gives: `app.dependency_overrides` is
    scoped to the test that sets it, whereas a patched global outlives a
    failing test.
    """
    return R2BodyStore()


def serialize(document: OpportunityDocument) -> dict:
    """What the form needs to describe one upload, and nothing more.

    `object_key` is deliberately absent: it is an internal storage address, and
    a client that knew it would be one presigning bug away from naming an
    object itself.
    """
    return {
        "id": str(document.id),
        "filename": document.filename,
        "content_type": document.content_type,
        "byte_size": document.byte_size,
        "extract_state": document.extract_state,
        "extract_error": document.extract_error,
        "prefill": document.prefill,
        "created_at": document.created_at.isoformat() if document.created_at else None,
    }


async def _read_within_limit(upload: UploadFile) -> bytes:
    """Read the upload, refusing anything over the configured size.

    Counted here as the bytes arrive rather than taken from `Content-Length`,
    which is the client's claim and which a streaming upload need not send at
    all. Reading one byte past the limit and stopping means a 10 GB post costs
    the limit plus a byte, not 10 GB.
    """
    limit = settings.OPPORTUNITY_DOCUMENT_MAX_UPLOAD_BYTES
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
    never reaches the object key — `opportunity_document_key` is computed — so
    this is presentation hygiene, not a security boundary.
    """
    name = PurePosixPath((raw or "").replace("\\", "/")).name.strip()
    return name[:_MAX_FILENAME] if name else f"job-description.{kind}"


async def _load_document(session, document_id: uuid.UUID) -> OpportunityDocument:
    """One document, tenant-scoped by RLS. 404 when it is not ours."""
    document = (
        await session.execute(
            select(OpportunityDocument).where(OpportunityDocument.id == document_id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.post("/opportunities/documents", status_code=201)
async def upload_document_no_opportunity(
    request: Request,
    file: Annotated[UploadFile, File()],
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """Accept a job-description file with no job order yet.

    The New job order dialog's upload half. The bytes are accepted the same
    hostile-until-proven way as a CV — computed key, sniffed type, size capped
    — and the row is parked at `pending` with `opportunity_id` NULL, because
    the vacancy does not exist until the recruiter reviews the extraction and
    presses Save. The extraction job reads the file and fills `prefill`; the
    form polls and pre-fills its fields from it.

    201, not 202: the row exists and its bytes are stored; only the reading of
    them happens afterwards.

    Declared before `/opportunities/{opportunity_id}/documents` is not enough
    on its own (different path depth), but the router is included before
    `opportunities` in `main.py` so the LITERAL `documents` segment is never
    swallowed by `/opportunities/{opportunity_id}`.
    """
    user_uuid, tenant_uuid, _ = await _require_session_with_role(request)

    # An upload buys an extraction job (up to 3 model calls), so it is
    # quota'd per agency per day like every other user-triggered LLM spend —
    # the module docstring's "deliberately no daily quota" dated from when
    # this was the only upload path; a programmatic client looping uploads
    # had an unbounded bill. Same COUNT(*)-since-midnight shape as the CV
    # quota, with the same accepted consequences.
    since = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    async with tenant_session(tenant_uuid) as session:
        used = (
            await session.execute(
                select(func.count())
                .select_from(OpportunityDocument)
                .where(OpportunityDocument.created_at >= since)
            )
        ).scalar_one()
    if used >= settings.OPPORTUNITY_DOCUMENT_DAILY_QUOTA:
        raise HTTPException(
            status_code=429,
            detail=(
                f"This agency has uploaded its "
                f"{settings.OPPORTUNITY_DOCUMENT_DAILY_QUOTA} "
                "job-description files for today. Try again tomorrow."
            ),
        )

    content = await _read_within_limit(file)

    from app.services.cv.convert import is_legacy_office
    from app.services.cv.text import sniff

    kind = sniff(content)
    if kind is None and is_legacy_office(content):
        # A .doc (Word 97-2003) is stored as-is and converted to .docx inside
        # the worker, where LibreOffice runs. Accepted here so the job sees it;
        # the route never shells out itself.
        kind = "doc"
    if kind is None:
        raise HTTPException(
            status_code=415,
            detail="Only PDF and Word (.doc/.docx) files can be read, whatever this was named.",
        )

    # A provisional id for the object key. The opportunity does not exist yet,
    # so there is no opportunity_id to hang the key off — a placeholder uuid is
    # minted here and the real one is written by `create_opportunity` when the
    # form saves. The document row keeps its own id regardless.
    provisional_opportunity_id = uuid.uuid4()
    document_id = uuid.uuid4()
    # Derived from the authenticated tenant and ids we mint, never from
    # anything the client sent. This is the line that makes cross-tenant
    # writes impossible.
    key = opportunity_document_key(
        tenant_uuid, provisional_opportunity_id, document_id, kind
    )
    await store.put_bytes(key, content, _MIME_FOR_KIND[kind])

    async with tenant_session(tenant_uuid) as session:
        document = OpportunityDocument(
            id=document_id,
            tenant_id=tenant_uuid,
            opportunity_id=None,
            filename=_safe_filename(file.filename, kind),
            content_type=_MIME_FOR_KIND[kind],
            byte_size=len(content),
            object_key=key,
            extract_state=OpportunityDocument.PENDING,
            uploaded_by=user_uuid,
        )
        session.add(document)
        await session.commit()

    # Enqueued after the commit, because the job reads the row it is named
    # for. `enqueue` returns a bool and never raises (`queue.py`), so a silent
    # Redis outage would otherwise leave this document `pending` until
    # `rescan_stuck` happened by — which is a sweep, not a promise. Say so on
    # the row instead: `failed` is the retryable terminal state, so the form
    # can let the recruiter remove the file rather than guessing.
    if not await enqueue(
        "extract_opportunity_document",
        tenant_id=str(tenant_uuid),
        document_id=str(document_id),
    ):
        log.warning(
            "opportunity_document_enqueue_failed", document_id=str(document_id)
        )
        async with tenant_session(tenant_uuid) as session:
            document = await session.get(OpportunityDocument, document_id)
            if document is not None:
                document.extract_state = OpportunityDocument.FAILED
                document.extract_error = _ENQUEUE_FAILED
                await session.commit()
                return serialize(document)

    async with tenant_session(tenant_uuid) as session:
        stored = await session.get(OpportunityDocument, document_id)
        if stored is None:  # pragma: no cover - deleted between two statements
            raise HTTPException(status_code=404, detail="Document not found")
        return serialize(stored)


@router.get("/opportunities/documents/{document_id}")
async def get_document(
    request: Request,
    document_id: uuid.UUID,
) -> dict:
    """The document row: extraction state and, once read, the prefill.

    The form polls this while `extract_state` is `pending`/`extracting` and
    reads `prefill` when it is `extracted`. Tenant-scoped by RLS — another
    agency's id is a 404, and no visibility predicate applies because at
    upload time the document belongs to nobody's job order yet.
    """
    _, tenant_uuid, _ = await _require_session_with_role(request)
    async with tenant_session(tenant_uuid) as session:
        return serialize(await _load_document(session, document_id))


@router.get("/opportunities/documents/{document_id}/download")
async def download_document(
    request: Request,
    document_id: uuid.UUID,
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """A short-lived URL the browser can fetch the original file from.

    Signed per request, after the tenant check, and never stored: a persisted
    URL would be a capability that outlives the permission it was granted
    under. Only served for a document linked to a job order the caller can
    see — an unlinked create-dialog upload has no vacancy to authorise it.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        document = await _load_document(session, document_id)
        if document.opportunity_id is None:
            raise HTTPException(status_code=404, detail="Document not found")
        await load_visible_opportunity(
            session, document.opportunity_id, user_uuid, role
        )
        key = document.object_key

    # The key was computed by us at upload and is not client-writable, but the
    # prefix is checked before signing anyway: without this, any future write
    # path that got a value into `object_key` would become a way to sign an
    # arbitrary object in the bucket, including another tenant's.
    if not key.startswith(f"{tenant_uuid}/"):  # pragma: no cover - defence in depth
        raise HTTPException(status_code=404, detail="Document not found")

    ttl = settings.OPPORTUNITY_DOCUMENT_PRESIGNED_URL_TTL_SECONDS
    return {"url": await store.presigned_get(key, ttl), "expires_in": ttl}


@router.delete("/opportunities/documents/{document_id}", status_code=204)
async def delete_document(
    request: Request,
    document_id: uuid.UUID,
    store: Annotated[BodyStore, Depends(body_store)],
) -> Response:
    """Remove the file: the object first, then the row.

    That order is deliberate, as with a CV. If the object delete fails the row
    still names it, so a retry can find it; the reverse leaves bytes in R2
    that nothing references and no sweep can locate.

    The row belongs to the tenant (RLS), and at create-dialog time to nobody's
    job order in particular, so there is no visibility predicate to apply — the
    uploader's own dialog is the only thing that names this id. A document
    already linked to a vacancy may only be removed by someone who can edit
    that vacancy: removing the source file is an edit to the job order, and a
    read-only share grants sight, not the right to destroy the vacancy's file.
    """
    user_uuid, tenant_uuid, role = await _require_session_with_role(request)

    async with tenant_session(tenant_uuid) as session:
        document = await _load_document(session, document_id)
        if document.opportunity_id is not None:
            await load_editable_opportunity(
                session, document.opportunity_id, user_uuid, role
            )
        keys = [document.object_key]

    await store.delete(*keys)

    async with tenant_session(tenant_uuid) as session:
        document = await session.get(OpportunityDocument, document_id)
        # Gone already — a double-clicked delete is not an error, and the row
        # was tenant-checked a moment ago in the transaction above.
        if document is not None:
            await session.delete(document)
            await session.commit()

    return Response(status_code=204)
