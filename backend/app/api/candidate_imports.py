"""A candidate spreadsheet: upload it, watch it, read its errors, undo it.

Its own module rather than more of `candidates.py`, and shaped like
`candidate_documents.py` — this is bytes arriving from a browser, to be
treated as hostile until proven otherwise. The rules that shape it are that
module's:

1. **The key is computed, never received.** `import_key` is built from the
   tenant on the session cookie and an id we mint, so a filename containing
   `../` cannot escape the `{tenant_id}/` prefix a tenant erasure purges by.
2. **Another agency's import is a 404, never a 403.** Every read goes through
   the tenant session, so a foreign id is simply not there.
3. **Nothing the client says about the file is believed.** Not the filename,
   not the Content-Type — `sniff_table` reads the bytes.

What is deliberately *not* here is the reading of the file. Parsing five
hundred rows and writing them is the job's work (`app.workers.import_jobs`);
this module accepts bytes, answers 202 and gets out of the way.
"""

import io
import uuid
from pathlib import PurePosixPath
from typing import Annotated

import openpyxl
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy import select

from app.api.auth import _require_session
from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import CandidateImport
from app.services.imports.rows import (
    CANDIDATE_HEADERS,
    CANDIDATE_SHEET,
    HISTORY_HEADERS,
    HISTORY_SHEET,
)
from app.services.imports.table import sniff_table
from app.services.imports.undo import SETTLED, undo_import
from app.services.storage.r2 import (
    BodyStore,
    R2BodyStore,
    import_error_report_key,
    import_key,
)
from app.workers.queue import enqueue

log = get_logger(__name__)

router = APIRouter(tags=["candidates"])

_SPREADSHEET_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# What each sniffed kind really is, so R2 serves the file back under a
# Content-Type that matches the bytes rather than under whatever the browser
# guessed from the extension.
_MIME_FOR_KIND = {"csv": "text/csv; charset=utf-8", "xlsx": _SPREADSHEET_MIME}

# Postgres would raise on a longer value anyway; truncating here means an
# absurd filename is a stored oddity rather than a 500.
_MAX_FILENAME = 255

# The two answers the `sheet` form field may give. A CSV is one sheet with no
# name of its own, so the uploader has to say which of the two it is; an XLSX
# carries both sheets already and the field is ignored.
_SHEETS = {CANDIDATE_SHEET.lower(): CANDIDATE_SHEET, HISTORY_SHEET.lower(): HISTORY_SHEET}

# The key stem for a workbook, which needs no sheet in its name because it
# holds both.
_WORKBOOK_STEM = "workbook"

_ENQUEUE_FAILED = (
    "This file was saved but could not be queued for import. Try again in a few minutes.\n"
)

_TEMPLATE_FILENAME = "candidate-import-template.xlsx"


def body_store() -> BodyStore:
    """Indirection point, so tests can swap in the in-memory store."""
    return R2BodyStore()


def serialize(record: CandidateImport) -> dict:
    """What the imports table needs to describe one run, and nothing more.

    `object_key` and `error_report_key` are deliberately absent: they are
    internal storage addresses, and a client that knew them would be one
    presigning bug away from naming an object itself. `has_errors` is the
    part the UI actually needs — whether to offer the report link.
    """
    return {
        "id": str(record.id),
        "filename": record.filename,
        "content_type": record.content_type,
        "byte_size": record.byte_size,
        "state": record.state,
        "candidates_created": record.candidates_created,
        "candidates_updated": record.candidates_updated,
        "roles_created": record.roles_created,
        "roles_updated": record.roles_updated,
        "rows_failed": record.rows_failed,
        "has_errors": record.error_report_key is not None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


async def _read_within_limit(upload: UploadFile) -> bytes:
    """Read the upload, refusing anything over the configured size.

    Counted as the bytes arrive rather than taken from `Content-Length`,
    which is the client's claim and which a streaming upload need not send at
    all. Reading one byte past the limit and stopping means a 10 GB post costs
    the limit plus a byte, not 10 GB.
    """
    limit = settings.IMPORT_MAX_UPLOAD_BYTES
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
    never reaches the object key — `import_key` is computed — so this is
    presentation hygiene, not a security boundary.
    """
    name = PurePosixPath((raw or "").replace("\\", "/")).name.strip()
    return name[:_MAX_FILENAME] if name else f"import.{kind}"


async def _load(session, import_id: uuid.UUID) -> CandidateImport:
    """One import, or a 404.

    Read through the tenant session, which is what makes another agency's id
    indistinguishable from one that never existed — the 404-not-403 rule.
    """
    record = (
        await session.execute(select(CandidateImport).where(CandidateImport.id == import_id))
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Import not found")
    return record


@router.get("/candidates/imports/template")
async def import_template() -> Response:
    """A workbook with the headers we read, and nothing else in it.

    Declared before the `{import_id}` routes below so `template` is never
    parsed as an id. The columns come from `rows.py`'s own constants rather
    than being typed out here, because a template naming a column the parser
    does not read is worse than no template at all: the recruiter fills it in
    and the value silently disappears.

    Returned as an XLSX rather than a CSV because it carries both sheets —
    the shape we would rather an agency send back.
    """
    workbook = openpyxl.Workbook()
    candidates = workbook.active
    candidates.title = CANDIDATE_SHEET
    candidates.append(list(CANDIDATE_HEADERS))
    history = workbook.create_sheet(HISTORY_SHEET)
    history.append(list(HISTORY_HEADERS))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type=_SPREADSHEET_MIME,
        headers={"Content-Disposition": f'attachment; filename="{_TEMPLATE_FILENAME}"'},
    )


@router.get("/candidates/imports")
async def list_imports(
    request: Request,
    limit: int | None = Query(default=None, ge=1),
) -> list[dict]:
    """Recent imports, newest first, so a migration is visible while it runs."""
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        statement = select(CandidateImport).order_by(CandidateImport.created_at.desc())
        if limit is not None:
            statement = statement.limit(limit)
        rows = (await session.execute(statement)).scalars().all()
        return [serialize(row) for row in rows]


@router.post("/candidates/imports", status_code=202)
async def upload_import(
    request: Request,
    file: Annotated[UploadFile, File()],
    store: Annotated[BodyStore, Depends(body_store)],
    sheet: Annotated[str, Form()] = CANDIDATE_SHEET,
) -> dict:
    """Accept a spreadsheet and queue it for import.

    202, not 201: the row exists but the answer does not. Nothing here reads
    the file — the row cap and every per-row problem are the job's to find,
    because a five-hundred-row apply has no business inside a request.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    named = _SHEETS.get(sheet.strip().lower())
    if named is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A CSV must say which sheet it is: {CANDIDATE_SHEET!r} or {HISTORY_SHEET!r}."
            ),
        )

    content = await _read_within_limit(file)

    # The bytes decide, not the extension and not the Content-Type. 415 rather
    # than 400: the request was well formed, the media type is what we refuse.
    kind = sniff_table(content)
    if kind is None:
        raise HTTPException(
            status_code=415,
            detail="Only CSV and Excel (.xlsx) files can be imported, whatever this was named.",
        )

    import_id = uuid.uuid4()
    # An XLSX names its own sheets, so the stem carries no routing decision;
    # a CSV has one nameless sheet, and the stem is where the job reads back
    # which of the two it is standing in for. Derived from the authenticated
    # tenant and an id we mint, never from anything the client sent.
    stem = _WORKBOOK_STEM if kind == "xlsx" else named.lower()
    key = import_key(tenant_uuid, import_id, stem, kind)
    await store.put_bytes(key, content, _MIME_FOR_KIND[kind])

    async with tenant_session(tenant_uuid) as session:
        record = CandidateImport(
            id=import_id,
            tenant_id=tenant_uuid,
            filename=_safe_filename(file.filename, kind),
            content_type=_MIME_FOR_KIND[kind],
            byte_size=len(content),
            object_key=key,
            state=CandidateImport.PENDING,
            uploaded_by=_user_uuid,
        )
        session.add(record)
        await session.commit()

    # Enqueued after the commit, because the job reads the row it is named
    # for. `enqueue` returns a bool and never raises (`queue.py`), so a silent
    # Redis outage would otherwise leave this import `pending` until
    # `rescan_stuck` happened by — which is a sweep, not a promise. Say so on
    # the row instead: `failed` is the retryable terminal state, so the panel
    # offers a retry rather than a spinner nobody is behind. The message goes
    # to the error report because an import has no error column — one place
    # to look, whatever went wrong.
    if not await enqueue(
        "run_candidate_import", tenant_id=str(tenant_uuid), import_id=str(import_id)
    ):
        log.warning("import_upload_enqueue_failed", candidate_import_id=str(import_id))
        report_key = import_error_report_key(tenant_uuid, import_id)
        await store.put_bytes(
            report_key, _ENQUEUE_FAILED.encode(), "text/plain; charset=utf-8"
        )
        async with tenant_session(tenant_uuid) as session:
            record = await _load(session, import_id)
            record.state = CandidateImport.FAILED
            record.error_report_key = report_key
            await session.commit()
            return serialize(record)

    async with tenant_session(tenant_uuid) as session:
        return serialize(await _load(session, import_id))


@router.get("/candidates/imports/{import_id}/errors")
async def import_errors(
    request: Request,
    import_id: uuid.UUID,
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """A short-lived URL the browser can fetch the error report from.

    Signed per request, after the tenant check, and never stored: the report
    names real candidates, so a persisted URL would be a capability that
    outlives the permission it was granted under.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        record = await _load(session, import_id)
        key = record.error_report_key

    if key is None:
        raise HTTPException(status_code=404, detail="This import reported no problems.")

    # The key was computed by us and is not client-writable, but the prefix is
    # checked before signing anyway: without this, any future write path that
    # got a value into `error_report_key` would become a way to sign an
    # arbitrary object in the bucket, including another tenant's.
    if not key.startswith(f"{tenant_uuid}/"):  # pragma: no cover - defence in depth
        raise HTTPException(status_code=404, detail="Import not found")

    ttl = settings.IMPORT_PRESIGNED_URL_TTL_SECONDS
    return {"url": await store.presigned_get(key, ttl), "expires_in": ttl}


@router.post("/candidates/imports/{import_id}/undo")
async def undo(request: Request, import_id: uuid.UUID) -> dict:
    """Walk one import back as far as it is still safe to.

    Idempotent by short-circuit rather than by running twice: a second call
    on an `undone` import would find every created row already gone and every
    restored field already restored, and would report that as a page of skips
    — technically harmless, and a confusing answer to give a recruiter who
    clicked once and lost the response.

    A run that has not settled is refused with 409 rather than 404: the import
    is the caller's own and the state is the objection, so telling them to
    wait is both true and actionable. `pending` is refused alongside
    `parsing`, because the job can claim a pending row between this check and
    the undo — the guarantee is not this gate but the conditional claim inside
    `undo_import`, and this gate is what turns losing that race into a
    sentence rather than an exception.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        record = await _load(session, import_id)
        if record.state == CandidateImport.UNDONE:
            return {"import": serialize(record), "already_undone": True}
        if record.state not in SETTLED:
            raise HTTPException(
                status_code=409,
                detail="This import has not finished. It can be undone once it has.",
            )

        outcome = await undo_import(session, tenant_id=tenant_uuid, import_id=import_id)
        # Read off the flushed row before committing. `undo_import` may have
        # deleted the rows this import created, and refreshing afterwards
        # would re-query under a session whose objects the commit expired.
        body = serialize(record)
        await session.commit()

        return {
            "import": body,
            "already_undone": False,
            "rows_deleted": outcome.rows_deleted,
            "fields_restored": outcome.fields_restored,
            "fields_skipped": outcome.fields_skipped,
            # The reasons, not just the count: "we kept 3 of your edits" is
            # only actionable if the recruiter can see which three.
            "skips": [
                {
                    "entity_type": skip.entity_type,
                    "entity_id": str(skip.entity_id),
                    "field_name": skip.field_name,
                    "reason": skip.reason,
                }
                for skip in outcome.skips
            ],
        }
