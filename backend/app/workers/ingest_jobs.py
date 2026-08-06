"""The arq job that reads a CV uploaded with no candidate named and resolves it.

The per-candidate upload path (`POST /candidates/{id}/documents`) hands the
document straight to `parse_candidate_cv`, because the recruiter already told
the platform who the CV belongs to. The ingest path (`POST /candidates/
documents`) does not — the recruiter dropped a CV in and the platform has to
work out the person itself. So this job is the front half `parse_candidate_cv`
does not have: read the document, extract the candidate's contact details,
match them to an existing person or create a new one, and only then hand off to
the same roles/skills parse the other path uses.

**The job carries its tenant**, like every other job here, for the reason
`jobs.py` gives — background work has no request and therefore no session
tenant, and a job naming a mismatched (tenant, row) pair reads no row under the
tenant policy and quietly does nothing.

**Identity is email-or-phone, never name** (`candidate_matching.find_candidate`).
A name is only a label. A CV that states neither an email nor a phone still
becomes a candidate — the placeholder row the route created holds it — but with
no contact details until a person edits it, and the roles/skills parse runs
regardless, because a CV with a career on it is useful the moment it is read
even before the recruiter fills the contact in.

**The hard case is a collision the uploader cannot see.** Two keys pointing at
two different people, or a single match held by a colleague, are decisions the
job must not make on its own: attaching the document to either would put one
person's career on the other's record. Those become `needs_review` and a person
looks at them. The roles/skills parse is not run while the candidate is in
dispute, so nothing the CV said reaches the wrong record first.
"""

import uuid

from sqlalchemy import delete, func, insert, select

from app.core.config import settings
from app.core.logging import get_logger
from app.db.rls import tenant_session
from app.models.candidate import Candidate, CandidateDocument
from app.models.tenant import User
from app.services.candidate_matching import find_candidate
from app.services.candidate_naming import normalize_email, normalize_phone
from app.services.cv.identity import extract_identity
from app.services.cv.ocr import OCRUnavailable, ocr_text
from app.services.cv.text import UnsupportedDocument, extract_text, sniff
from app.services.llm.client import LLMInvalidJSON
from app.services.storage.r2 import R2BodyStore
from app.services.visibility import visible_candidates
from app.workers.queue import enqueue

log = get_logger(__name__)

# The states this job may legitimately start from. `ingesting` is included so a
# worker killed mid-call — leaving the row there — is picked up by `rescan_stuck`
# rather than stranded: the resolver routes `ingest_pending` and `ingesting` to
# this job, and the row is idempotent across a replay because identity
# extraction re-resolves to the same person.
_RESUMABLE = (CandidateDocument.INGEST_PENDING, CandidateDocument.INGESTING)

# A display name for a CV that stated none the model could trust, or none at
# all. Kept short and recognisable so the recruiter can find the row in the list
# and edit the contact in.
_UNNAMED = "Uploaded CV"


def body_store():
    """Indirection point, so tests can swap in the in-memory store."""
    return R2BodyStore()


# Injectable OCR seam — see `cv_jobs.ocr_extract` for the rationale. Shared name
# would couple the two modules; each holds its own so a test patches the one it
# is exercising.
ocr_extract = ocr_text


async def _terminal(
    tenant: uuid.UUID,
    document_id: uuid.UUID,
    state: str,
    error: str,
) -> None:
    """Park the document in a terminal state with a sentence saying why."""
    async with tenant_session(tenant) as session:
        document = await session.get(CandidateDocument, document_id)
        if document is None:
            return
        document.parse_state = state
        document.parse_error = error[:2000]


async def _resolve_candidate(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    full_name: str | None,
    email: str | None,
    phone_raw: str | None,
    phone_e164: str | None,
) -> tuple[uuid.UUID | None, str | None]:
    """Match these details to an existing candidate, or create one.

    Returns `(candidate_id, review_reason)`:
    - `(id, None)` — resolved. Either an existing candidate the caller may see,
      or a freshly created one.
    - `(None, reason)` — not resolved; a person must decide. The reason is shown
      on the document row.

    Identity is email-or-phone (`find_candidate`); a name is never a key. A
    collision — two keys at two people, or a single match held by a colleague —
    is returned as a review reason rather than a guess. The unique indexes on
    `candidates` are the backstop for a race the matcher's read could not see.
    """
    match = await find_candidate(session, tenant_id, email, phone_e164)
    if match.conflict is not None:
        a, b = match.conflict
        return None, (
            f"This CV's email and phone belong to two different candidates "
            f"({a} and {b}). Merge them first, or correct the details."
        )
    if match.candidate_id is not None:
        # A match the uploader can see is the ordinary duplicate: attach the
        # document to it. A match they cannot see — held by a colleague under a
        # visibility rule — is not ours to attach silently; flag it for review.
        visible = (
            await session.execute(
                select(Candidate.id)
                .where(Candidate.id == match.candidate_id)
                .where(visible_candidates(user_id, role))
            )
        ).scalar_one_or_none()
        if visible is not None:
            return visible, None
        holder = (
            await session.execute(
                select(
                    func.coalesce(User.preferred_name, User.display_name, User.email)
                )
                .select_from(Candidate)
                .join(User, User.id == Candidate.owner_id)
                .where(Candidate.id == match.candidate_id)
            )
        ).scalar_one_or_none()
        whom = holder or "a colleague"
        return None, (
            f"This CV matches a candidate held by {whom}. "
            "Request access or confirm the details before attaching it."
        )

    # No match: a new person. The uploader is the owner, the same rule the
    # manual create path applies ("you uploaded it, it is yours"). `full_name`
    # may be None when the model could not read one; the candidate still gets a
    # row, the recruiter fills the name in.
    candidate_id = uuid.uuid4()
    await session.execute(
        insert(Candidate).values(
            id=candidate_id,
            tenant_id=tenant_id,
            full_name=(full_name or _UNNAMED)[:1000],
            email=email,
            phone_raw=phone_raw,
            phone_e164=phone_e164,
            pipeline_stage=Candidate.STAGES[0],
            record_status=Candidate.ACTIVE,
            owner_id=user_id,
            created_by=user_id,
            updated_by=user_id,
        )
    )
    return candidate_id, None


async def ingest_candidate_cv(
    ctx, *, tenant_id: str, document_id: str
) -> None:
    """Read one CV's identity and resolve it to a candidate, then hand off to parse.

    The document was stored by `POST /candidates/documents` against a placeholder
    candidate row (the foreign key is NOT NULL), so this job's first move is to
    load the bytes, not to worry about who owns them. Identity extraction runs
    on the same extracted text the roles/skills parse will index its evidence
    spans into, so the two never disagree about what the CV said.

    Failure discipline mirrors `parse_candidate_cv`. The row moves to
    `ingesting` before the model call; `unreadable` and `needs_review` are
    terminal and never retried; `failed` is the retryable terminal state for an
    unusable model answer.
    """
    if not settings.cerebras_configured(settings.EXTRACTION_MODEL_FAST):
        log.error(
            "llm_not_configured",
            job="ingest_candidate_cv",
            detail="Set CEREBRAS_BASE_URL, CEREBRAS_API_KEY and EXTRACTION_MODEL_FAST.",
        )
        raise RuntimeError("CV ingest has no model configured.")

    tenant = uuid.UUID(tenant_id)
    document = uuid.UUID(document_id)

    async with tenant_session(tenant) as session:
        row = (
            await session.execute(
                select(CandidateDocument).where(CandidateDocument.id == document)
            )
        ).scalar_one_or_none()
        if row is None:
            log.info("cv_ingest_skipped_unknown_row", candidate_document_id=document_id)
            return
        if row.parse_state not in _RESUMABLE:
            log.info(
                "cv_ingest_skipped_already_answered",
                candidate_document_id=document_id,
                parse_state=row.parse_state,
            )
            return
        placeholder_id = row.candidate_id
        object_key = row.object_key
        user_id = row.uploaded_by
        filename = row.filename
        row.parse_state = CandidateDocument.INGESTING
        row.parse_error = None

    if user_id is None:
        # The route always sets `uploaded_by`; reaching here means a row was
        # written by a path that skipped it, and we cannot resolve ownership.
        await _terminal(
            tenant, document, CandidateDocument.FAILED,
            "This document has no uploader, so its candidate could not be resolved.",
        )
        return

    # The role travels with the user so a colleague-held match can be detected.
    # Read once, outside the row's transaction.
    async with tenant_session(tenant) as session:
        role = (
            await session.execute(select(User.role).where(User.id == user_id))
        ).scalar_one_or_none() or "recruiter"

    store = body_store()
    data = await store.get_bytes(object_key)
    if not data:
        await _terminal(
            tenant, document, CandidateDocument.UNREADABLE,
            "The uploaded file could not be found in storage, so there was "
            "nothing to read. Please upload it again.",
        )
        return

    kind = sniff(data)
    if kind is None:
        await _terminal(
            tenant, document, CandidateDocument.UNREADABLE,
            "This file is neither a PDF nor a Word document, whatever it was "
            "named, so there was nothing to read.",
        )
        return

    try:
        source = extract_text(data, kind, max_chars=settings.CV_TEXT_MAX_CHARS)
    except UnsupportedDocument as exc:
        await _terminal(
            tenant, document, CandidateDocument.UNREADABLE,
            f"This {kind.upper()} could not be read: {exc}",
        )
        return

    if not source.strip():
        # A scanned CV yields no text layer; OCR recovers it when configured, so
        # the identity read that follows sees real contact details rather than
        # an empty string. Same fallback shape as `parse_candidate_cv`.
        if settings.ocr_configured():
            try:
                ocrd = await ocr_extract(
                    data,
                    languages=settings.CV_OCR_LANGUAGES,
                    max_pages=settings.CV_OCR_MAX_PAGES,
                    timeout=settings.CV_OCR_TIMEOUT_SECONDS,
                )
            except OCRUnavailable as exc:
                await _terminal(
                    tenant, document, CandidateDocument.UNREADABLE,
                    f"This scanned CV could not be read by OCR: {exc}",
                )
                return
            if not ocrd.strip():
                await _terminal(
                    tenant, document, CandidateDocument.UNREADABLE,
                    "No text could be read from this file, even after OCR. The "
                    "scan may be too faint, low-resolution, or of a page with no "
                    "text on it.",
                )
                return
            source = ocrd
        else:
            await _terminal(
                tenant, document, CandidateDocument.UNREADABLE,
                "No text could be read from this file. A scanned or photographed "
                "CV has no text layer to extract; a text-based PDF or Word file "
                "can be read.",
            )
            return

    try:
        identity, _ = await extract_identity(source)
    except LLMInvalidJSON as exc:
        log.warning("cv_ingest_identity_failed", candidate_document_id=document_id, error=str(exc))
        await _terminal(
            tenant, document, CandidateDocument.FAILED,
            f"Reading this CV's contact details did not produce a usable answer: {exc}",
        )
        return

    full_name = identity.full_name.value if identity.full_name else None
    raw_email = identity.email.value if identity.email else None
    raw_phone = identity.phone.value if identity.phone else None
    email = normalize_email(raw_email) if raw_email else None
    phone_e164 = normalize_phone(raw_phone) if raw_phone else None

    async with tenant_session(tenant) as session:
        candidate_id, review_reason = await _resolve_candidate(
            session,
            tenant_id=tenant,
            user_id=user_id,
            role=role,
            full_name=full_name,
            email=email,
            phone_raw=raw_phone,
            phone_e164=phone_e164,
        )
        if candidate_id is None:
            # A collision or a colleague-held match. Terminal: a person decides.
            row = await session.get(CandidateDocument, document)
            if row is not None:
                row.parse_state = CandidateDocument.NEEDS_REVIEW
                row.parse_error = review_reason
            await session.commit()
            return

        # Bind the document to the resolved candidate. If it differs from the
        # placeholder the route created, the placeholder is now an empty,
        # unowned row; delete it so it does not linger in the candidate list as
        # a ghost. Identity-only — the parse has not run yet. The re-bind and
        # the ghost delete land in one transaction: a partial commit would
        # leave the document attached to its new candidate while the empty
        # placeholder lingered, which is exactly the ghost this prevents.
        row = await session.get(CandidateDocument, document)
        if row is None:
            return
        old_candidate_id = row.candidate_id
        row.candidate_id = candidate_id
        row.parse_state = CandidateDocument.PENDING
        row.parse_error = None
        if old_candidate_id != candidate_id:
            await _delete_if_ghost(session, tenant, old_candidate_id, keep=document)
        await session.commit()

    # Hand off to the roles/skills parse. Enqueued after the commit, because the
    # job reads the row it is named for. The document is now an ordinary
    # `pending` row, indistinguishable from one the per-candidate path produced.
    if not await enqueue(
        "parse_candidate_cv",
        tenant_id=str(tenant),
        candidate_id=str(candidate_id),
        document_id=str(document),
    ):
        log.warning("cv_ingest_handoff_enqueue_failed", candidate_document_id=document_id)
        async with tenant_session(tenant) as session:
            row = await session.get(CandidateDocument, document)
            if row is not None:
                row.parse_state = CandidateDocument.FAILED
                row.parse_error = _ENQUEUE_FAILED
                await session.commit()


_ENQUEUE_FAILED = (
    "This file was read but could not be queued for parsing. Try again in a few minutes.\n"
)


async def _delete_if_ghost(session, tenant: uuid.UUID, candidate_id: uuid.UUID, *, keep: uuid.UUID) -> None:
    """Delete a placeholder candidate left empty by re-binding its document.

    The route created a minimal candidate row to satisfy the NOT NULL foreign
    key before identity was read. When identity resolves to a *different*
    candidate (a match), the placeholder owns no document and holds no facts,
    so leaving it would seed the candidate list with a ghost per ingest. This
    removes it — only when it is genuinely empty: no documents other than the
    one just re-bound, and the default `new` pipeline stage with no contact
    details, the exact shape the route wrote.

    Runs on the caller's session inside its transaction, so the delete and the
    re-bind land together.
    """
    row = (
        await session.execute(
            select(Candidate).where(
                Candidate.id == candidate_id,
                Candidate.tenant_id == tenant,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    # Only a row the route created — no contact details, default stage, owned by
    # nobody in particular (the uploader owns it, but so does every placeholder).
    # A recruiter who edited the placeholder before the job ran would have added
    # a detail or changed the stage, and this guard keeps that edit.
    if row.email is not None or row.phone_e164 is not None:
        return
    if row.pipeline_stage != Candidate.STAGES[0]:
        return
    if row.full_name and row.full_name != _UNNAMED:
        # The model read a name and we stored it on the placeholder before
        # re-binding — that is real data, not a ghost. Keep the row.
        return
    other_docs = (
        await session.execute(
            select(func.count())
            .select_from(CandidateDocument)
            .where(
                CandidateDocument.candidate_id == candidate_id,
                CandidateDocument.id != keep,
            )
        )
    ).scalar_one()
    if other_docs:
        return
    await session.execute(
        delete(Candidate).where(
            Candidate.id == candidate_id,
            Candidate.tenant_id == tenant,
        )
    )
