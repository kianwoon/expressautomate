"""The arq job that reads an uploaded job-description document.

Its own module rather than a function in `app.workers.jobs`: that file is at
the repo's 1500-line ceiling, and a job-description extraction shares nothing
with mail ingestion but the queue it arrives on and the extraction prompt.

**The job carries its tenant**, like every other job here, for the reason
`jobs.py` gives — background work has no request and therefore no session
tenant, and a job naming a mismatched (tenant, row) pair reads no row under
the tenant policy and quietly does nothing.

**The same model call as email extraction.** `ingest.extract` runs the
identical prompt and verification the mail pipeline uses, so a document-derived
prefill inherits the anti-fabrication discipline (§15) for free: the model must
quote the source text, `evidence.py` checks the quotes deterministically, and a
value the document never mentions comes back as `Not mentioned` → null.

**A parse is bounded in wall clock**, exactly as `parse_candidate_cv` is: a
single-page FlateDecode bomb still inflates inside `pypdf` where nothing of
ours is watching, and a model call can hang. `settings.py` registers this
function with `OPPORTUNITY_DOCUMENT_EXTRACT_TIMEOUT_SECONDS` as arq's job
timeout, and `rescan_stuck` recovers a timed-out row from `extracting`.
"""

import uuid

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models import OpportunityDocument
from app.services.cv.convert import (
    ConversionUnavailable,
    is_legacy_office,
    maybe_convert,
)
from app.services.cv.text import UnsupportedDocument, extract_text, sniff
from app.services.ingest.extract import extract
from app.services.ingest.schema import ExtractedField, ExtractedJob
from app.services.llm.client import LLMInvalidJSON
from app.services.storage.r2 import R2BodyStore

log = get_logger(__name__)

# The states a parse may legitimately start from. `extracting` is included
# deliberately: a worker killed mid-call leaves the row there and
# `rescan_stuck` re-enqueues exactly this job for it, so accepting only
# `pending` would strand that document forever. Every other state is an
# answer — replaying the job on an `extracted` document must change nothing.
_RESUMABLE = (OpportunityDocument.PENDING, OpportunityDocument.EXTRACTING)


def body_store():
    """Indirection point, so tests can swap in the in-memory store."""
    return R2BodyStore()


async def _terminal(
    tenant: uuid.UUID, document_id: uuid.UUID, state: str, error: str
) -> None:
    """Park the document in a terminal state with a sentence saying why.

    Truncated because `extract_error` is read by a person, and an unbounded
    parser message would otherwise put a chunk of a document in a column meant
    for one line of diagnosis.
    """
    async with tenant_session(tenant) as session:
        document = await session.get(OpportunityDocument, document_id)
        if document is None:
            return
        document.extract_state = state
        document.extract_error = error[:2000]


def _prefill(job: ExtractedJob) -> dict:
    """Map one extracted vacancy onto the create-dialog form's vocabulary.

    `Not mentioned` becomes absent from the JSON — never a fabricated value
    and never an empty string, so a field the document did not state is
    indistinguishable from one the form left untouched. Only the first job of
    the document is used; a job description normally describes one vacancy,
    and a multi-vacancy document is a future concern.

    The form's fields are the `_raw` strings plus the two free-text columns,
    exactly what `ManualOpportunityRequest` accepts. `company` maps to the
    form's client search box's company-name fallback.
    """
    def value(field: ExtractedField | None) -> str | None:
        if field is None or field.is_missing:
            return None
        # The model can answer 2500 as an integer; the schema coerces it to a
        # string in the same breath (`ExtractedField._coerce_numeric_value`).
        return str(field.value)

    return {
        "job_title_raw": value(job.job_title),
        "company_name_raw": value(job.company),
        "location_raw": value(job.location),
        "salary_raw": value(job.salary),
        "working_hours_raw": value(job.working_hours),
        "duration_raw": value(job.duration),
        "employment_type": value(job.employment_type),
        "job_description": value(job.job_description),
        "requirements": value(job.requirements),
    }


async def extract_opportunity_document(
    ctx, *, tenant_id: str, document_id: str
) -> None:
    """Read one uploaded job-description file and fill its prefill.

    Failure discipline mirrors `parse_candidate_cv`. The row moves to
    `extracting` before the model call, because arq only reschedules on
    `Retry` and nothing here raises one: an infrastructure failure is a
    permanently failed job and `rescan_stuck` re-enqueues the row once the
    outage ends. Leaving it at `pending` across the call would instead let the
    sweep pay for the same extraction while the first attempt was still in
    flight.

    `unreadable` is never retried. A scanned page with no text layer, an
    encrypted PDF, a file that is not the type it claims — all answer the same
    way however many times they are asked, so the state is terminal and the UI
    can stop offering a retry that cannot work.
    """
    # Asked once, before the row is touched, for the reason `extract_email`
    # gives: an unconfigured deployment must fail loudly here rather than mark
    # every document `failed` one httpx error at a time.
    if not settings.deepseek_configured(settings.EXTRACTION_MODEL_FAST):
        log.error(
            "llm_not_configured",
            job="extract_opportunity_document",
            detail="Set DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY and EXTRACTION_MODEL_FAST.",
        )
        raise RuntimeError("Job-description extraction has no model configured.")

    tenant = uuid.UUID(tenant_id)
    document_uuid = uuid.UUID(document_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                select(OpportunityDocument).where(
                    OpportunityDocument.id == document_uuid
                )
            )
        ).scalar_one_or_none()
        if row is None:
            # Unknown row, or a job whose tenant does not own it. RLS already
            # decided; there is nothing to do and nothing to report.
            log.info(
                "opportunity_document_extract_skipped_unknown_row",
                document_id=document_id,
            )
            return
        if row.extract_state not in _RESUMABLE:
            log.info(
                "opportunity_document_extract_skipped_already_answered",
                document_id=document_id,
                extract_state=row.extract_state,
            )
            return
        object_key = row.object_key
        row.extract_state = OpportunityDocument.EXTRACTING
        row.extract_error = None

    store = body_store()
    data = await store.get_bytes(object_key)
    if not data:
        # The row says there is a file and storage does not have it. Terminal
        # rather than retried: nothing about waiting puts the bytes back, and
        # `failed` would leave a retry button that can only ever fail again.
        await _terminal(
            tenant,
            document_uuid,
            OpportunityDocument.UNREADABLE,
            "The uploaded file could not be found in storage, so there was "
            "nothing to read. Please upload it again.",
        )
        return

    # The bytes decide, not the extension. A `.doc` (Word 97-2003) is not the
    # .docx zip `sniff` recognises, but it is a real Office document the
    # converter can rescue. Convert and re-sniff; the rest of the pipeline
    # reads the result as an ordinary .docx. A missing converter surfaces as
    # the same named refusal the CV job gives — never as a stranded
    # `extracting` row that `rescan_stuck` re-enqueues forever.
    kind = sniff(data)
    if kind is None and is_legacy_office(data):
        if settings.conversion_configured():
            try:
                data, kind = await maybe_convert(data, kind=kind)
            except ConversionUnavailable as exc:
                await _terminal(
                    tenant,
                    document_uuid,
                    OpportunityDocument.UNREADABLE,
                    f"This legacy document could not be converted: {exc}",
                )
                return
        else:
            await _terminal(
                tenant,
                document_uuid,
                OpportunityDocument.UNREADABLE,
                "This is a legacy Word (.doc) file. Save it as .docx or PDF and "
                "upload it again — the reader handles those directly.",
            )
            return
    if kind is None:
        # Not a PDF, not a DOCX, not a legacy Word file the converter could
        # rescue. Terminal, not retried: the bytes answer the same way forever.
        await _terminal(
            tenant,
            document_uuid,
            OpportunityDocument.UNREADABLE,
            "Only PDF and Word (.doc/.docx) files can be read, whatever this "
            "was named.",
        )
        return

    try:
        source = extract_text(data, kind, max_chars=settings.EXTRACTION_MAX_CHARS)
    except UnsupportedDocument as exc:
        # A corrupt or hostile file. `failed`, not `unreadable`: this is a
        # parser refusal, and a retry after the file is re-uploaded is the
        # honest answer — but for a file that is genuinely not a PDF/Word the
        # terminal `unreadable` is the one that keeps the UI from offering a
        # useless retry.
        await _terminal(
            tenant,
            document_uuid,
            OpportunityDocument.UNREADABLE,
            str(exc),
        )
        return

    if not source.strip():
        # A scanned page with no text layer. Same terminal state and sentence
        # as the CV path: retrying cannot change the bytes.
        await _terminal(
            tenant,
            document_uuid,
            OpportunityDocument.UNREADABLE,
            "No text could be read from this file — it looks like a scan. "
            "Upload a PDF or Word file that contains text, or type the "
            "vacancy in by hand.",
        )
        return

    try:
        response, _result = await extract(source)
    except LLMInvalidJSON as exc:
        # Both models were asked and neither answered in the required shape.
        # Retrying would spend the same tokens on the same document for the
        # same result, so the row is marked and the recruiter can either
        # re-upload or type by hand.
        log.warning(
            "opportunity_document_extraction_failed",
            document_id=document_id,
            error=str(exc),
        )
        await _terminal(
            tenant,
            document_uuid,
            OpportunityDocument.FAILED,
            "We could not read the vacancy out of this file. Remove it and "
            "try again, or type the job order in by hand.",
        )
        return

    first = response.jobs[0] if response.jobs else None
    async with tenant_session(tenant) as session:
        document = await session.get(OpportunityDocument, document_uuid)
        if document is None:  # pragma: no cover - deleted mid-extraction
            return
        document.extract_state = OpportunityDocument.EXTRACTED
        document.extract_error = None
        # A document with no vacancy in it is a successful outcome, not a
        # failure: the form simply has nothing to prefill and the recruiter
        # types the vacancy in by hand. The file stays attached for reference.
        document.prefill = _prefill(first) if first is not None else {}
