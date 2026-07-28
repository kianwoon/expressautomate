"""The arq job that reads an uploaded CV (plan §7, §13).

Its own module rather than a fifteenth function in `app.workers.jobs`: that
file is at the repo's 1500-line ceiling, and a CV parse shares nothing with
mail ingestion but the queue it arrives on.

**The job carries its tenant**, like every other job here, for the reason
`jobs.py` gives — background work has no request and therefore no session
tenant, and a job naming a mismatched (tenant, row) pair reads no row under
the tenant policy and quietly does nothing.

**A parse is bounded in wall clock.** `app.services.cv.text` bounds DOCX
inflation and stops PDF text accumulating between pages, but a single-page
FlateDecode bomb still inflates inside `pypdf`, where nothing of ours is
watching, and a model call can hang. `settings.py` registers this function
with `CV_PARSE_TIMEOUT_SECONDS` as arq's job timeout, so one hostile document
costs a worker slot for a bounded time rather than for the life of the
process. A timed-out job leaves the row at `parsing`, which `rescan_stuck`
picks up — the cost of a genuinely slow CV is a retry, not a lost one.
"""

import uuid

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import CandidateDocument
from app.services.cv.extract import extract_cv
from app.services.cv.persist import persist_cv
from app.services.cv.text import UnsupportedDocument, extract_text, sniff
from app.services.llm.client import LLMInvalidJSON
from app.services.storage.r2 import R2BodyStore, document_text_key

log = get_logger(__name__)

# The states a parse may legitimately start from. `parsing` is included
# deliberately: a worker killed mid-call leaves the row there and
# `rescan_stuck` re-enqueues exactly this job for it, so accepting only
# `pending` would strand that document forever. Every other state is an
# answer — replaying the job on a `parsed` document must change nothing, and
# re-reading an `unreadable` one gets the same answer at the same cost.
_RESUMABLE = (CandidateDocument.PENDING, CandidateDocument.PARSING)


def body_store():
    """Indirection point, so tests can swap in the in-memory store."""
    return R2BodyStore()


async def _terminal(
    tenant: uuid.UUID, document_id: uuid.UUID, state: str, error: str,
    text_key: str | None = None,
) -> None:
    """Park the document in a terminal state with a sentence saying why.

    Truncated because `parse_error` is read by a person, and an unbounded
    parser message would otherwise put a chunk of a document in a column meant
    for one line of diagnosis.

    `text_key` is only passed when the extracted text was already written to
    R2 before the failure — an invalid-JSON model answer, say. Recording it
    here is what lets a later delete of this document find that object;
    without it the row has no memory of a key it wrote, and the object
    outlives the row with nothing left that names it.
    """
    async with tenant_session(tenant) as session:
        document = await session.get(CandidateDocument, document_id)
        if document is None:
            return
        document.parse_state = state
        document.parse_error = error[:2000]
        if text_key is not None:
            document.text_key = text_key


async def parse_candidate_cv(
    ctx, *, tenant_id: str, candidate_id: str, document_id: str
) -> None:
    """Read one uploaded CV and record what it said.

    Failure discipline mirrors `extract_email`. The row moves to `parsing`
    before the model call, because arq only reschedules on `Retry` and nothing
    here raises one: an infrastructure failure is a permanently failed job and
    `rescan_stuck` re-enqueues the row once the outage ends. Leaving it at
    `pending` across the call would instead let the sweep pay for the same
    extraction while the first attempt was still in flight.

    `unreadable` is never retried. A scanned page with no text layer, an
    encrypted PDF, a file that is not the type it claims — all answer the same
    way however many times they are asked, so the state is terminal and the UI
    can stop offering a retry that cannot work.
    """
    # Asked once, before the row is touched, for the reason `extract_email`
    # gives: an unconfigured deployment must fail loudly here rather than mark
    # every CV `failed` one httpx error at a time. `EXTRACTION_MODEL_STRONG`
    # is not required — `extract_cv` falls back to the fast model at a higher
    # reasoning effort, and demanding it would refuse a deployment that parses
    # perfectly well.
    if not settings.cerebras_configured(settings.EXTRACTION_MODEL_FAST):
        log.error(
            "llm_not_configured",
            job="parse_candidate_cv",
            detail="Set CEREBRAS_BASE_URL, CEREBRAS_API_KEY and EXTRACTION_MODEL_FAST.",
        )
        raise RuntimeError("CV parsing has no model configured.")

    tenant = uuid.UUID(tenant_id)
    candidate = uuid.UUID(candidate_id)
    document = uuid.UUID(document_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                select(CandidateDocument).where(
                    CandidateDocument.id == document,
                    CandidateDocument.candidate_id == candidate,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            # Unknown row, or a job whose tenant does not own it. RLS already
            # decided; there is nothing to do and nothing to report.
            log.info("cv_parse_skipped_unknown_row", candidate_document_id=document_id)
            return
        if row.parse_state not in _RESUMABLE:
            log.info(
                "cv_parse_skipped_already_answered",
                candidate_document_id=document_id,
                parse_state=row.parse_state,
            )
            return
        object_key = row.object_key
        row.parse_state = CandidateDocument.PARSING
        row.parse_error = None

    store = body_store()
    data = await store.get_bytes(object_key)
    if not data:
        # The row says there is a file and storage does not have it. Terminal
        # rather than retried: nothing about waiting puts the bytes back, and
        # `failed` would leave a retry button that can only ever fail again.
        await _terminal(
            tenant,
            document,
            CandidateDocument.UNREADABLE,
            "The uploaded file could not be found in storage, so there was "
            "nothing to read. Please upload it again.",
        )
        return

    kind = sniff(data)
    if kind is None:
        await _terminal(
            tenant,
            document,
            CandidateDocument.UNREADABLE,
            "This file is neither a PDF nor a Word document, whatever it was "
            "named, so there was nothing to read.",
        )
        return

    try:
        # `max_chars` comes from configuration and is required: it is the
        # bound that stops a document inflating without limit, and it is also
        # the length of the string every evidence offset indexes into.
        source = extract_text(data, kind, max_chars=settings.CV_TEXT_MAX_CHARS)
    except UnsupportedDocument as exc:
        await _terminal(
            tenant,
            document,
            CandidateDocument.UNREADABLE,
            f"This {kind.upper()} could not be read: {exc}",
        )
        return

    if not source.strip():
        # A PDF of scanned images parses cleanly and yields nothing. Asking a
        # model about an empty string would bill for a confident nothing.
        await _terminal(
            tenant,
            document,
            CandidateDocument.UNREADABLE,
            "No text could be read from this file. A scanned or photographed "
            "CV has no text layer to extract; a text-based PDF or Word file "
            "can be read.",
        )
        return

    # Stored before the model is asked, and stored rather than re-derived,
    # because every evidence span this run records is an offset into exactly
    # this string.
    text_key = document_text_key(tenant, candidate, document)
    await store.put(text_key, source)

    try:
        response, result = await extract_cv(source)
    except LLMInvalidJSON as exc:
        # Both passes were made and neither answered in the required shape.
        # `failed`, not `unreadable`: the document was read perfectly well, and
        # a model or prompt fixed tomorrow makes the same file work.
        log.warning("cv_extraction_failed", candidate_document_id=document_id, error=str(exc))
        await _terminal(
            tenant,
            document,
            CandidateDocument.FAILED,
            f"Reading this CV did not produce a usable answer: {exc}",
            text_key=text_key,
        )
        return

    # One transaction for the text bookkeeping, everything the parse found,
    # and the document's move out of `parsing`. A partial commit would look
    # exactly like a complete answer to everything downstream.
    async with tenant_session(tenant) as session:
        row = await session.get(CandidateDocument, document)
        if row is None:
            # Deleted while the model was thinking. Nothing to attach the
            # extraction to, and `extractions.candidate_document_id` cascades
            # from a row that no longer exists.
            log.info("cv_parse_document_vanished", candidate_document_id=document_id)
            return
        row.text_key = text_key
        row.text_chars = len(source)
        row.parse_error = None
        await persist_cv(
            session,
            tenant_id=tenant,
            candidate_id=candidate,
            document=row,
            response=response,
            result=result,
            text=source,
        )
