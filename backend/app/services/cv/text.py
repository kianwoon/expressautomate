"""Extract text from CV files (PDF and DOCX)."""

import io
import zipfile

from docx import Document
from pypdf import PdfReader

from app.services.archive import BoundedArchiveTooLarge, bounded_archive

# A multiple, not a hardcoded byte count: the caller's max_chars already
# encodes the app's configured limit, and we only need enough headroom to
# admit legitimately verbose documents (rich XML markup around a modest
# amount of text) while still rejecting a zip that inflates without bound.
# No `settings` import here — this module stays pure and the actual limit
# always comes from the caller.
_DOCX_BOMB_MULTIPLIER = 100

# A floor under the multiplier above. A stock DOCX's XML payload (styles,
# theme, fonts) runs to roughly 800 KB of fixed overhead regardless of how
# little body text it holds, so coupling the budget purely to
# max_chars * _DOCX_BOMB_MULTIPLIER means any configured max_chars below
# ~8,000 rejects every legitimate Word document, not just bombs. Do NOT
# delete this floor to "simplify" the budget expression — that silently
# reintroduces the bug where a small, real-world text limit makes every
# DOCX unreadable. 5 MB is generous for any real CV and still nowhere near
# an availability risk to the shared worker.
_DOCX_MIN_BUDGET_BYTES = 5 * 1024 * 1024


class UnsupportedDocument(Exception):
    """Raised when a file cannot be parsed despite being of a claimed kind."""

    pass


def sniff(data: bytes) -> str | None:
    """Detect file type from its contents.

    Returns "pdf", "docx", or None. Never trusts the filename or
    Content-Type header — only examines the bytes themselves.
    """
    # PDF starts with %PDF-
    if data.startswith(b"%PDF-"):
        return "pdf"

    # DOCX is a zip, but we don't accept any zip — only those containing
    # word/document.xml, to reject archives of unrelated content.
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except (zipfile.BadZipFile, RuntimeError):
            # Not a valid zip or other zip errors; treat as unknown
            pass

    return None


def extract_text(data: bytes, kind: str, *, max_chars: int) -> str:
    """Extract text from a file of the given kind.

    A PDF or DOCX with no extractable text (e.g. a scan) returns "".
    Corrupt files raise UnsupportedDocument rather than leaking library errors.

    `max_chars` is not a formatting nicety — it bounds the work this function
    is allowed to do. This runs in a shared arq worker, so a hostile or
    malformed upload that forces unbounded decompression (a small DOCX whose
    document.xml inflates to hundreds of MB) or unbounded page iteration (a
    PDF with tens of thousands of pages) is a memory/availability risk to
    every tenant on that worker, not just the uploader. The caller supplies
    the bound from configuration; this module has no `settings` import and
    no opinion on what the number should be — it only enforces whatever it
    is given.

    Args:
        data: File bytes.
        kind: One of "pdf" or "docx".
        max_chars: Maximum number of characters to return. Required so the
            caller's configured limit is always the one enforced.

    Returns:
        Extracted text, possibly empty, never longer than max_chars.

    Raises:
        UnsupportedDocument: If the file cannot be parsed, or (for DOCX) if
            it actually decompresses past the bound derived from max_chars.
            Note "actually": the archive's own claims about its size are
            never consulted, because the attacker writes them.
    """
    if kind == "pdf":
        return _extract_pdf(data, max_chars=max_chars)
    elif kind == "docx":
        return _extract_docx(data, max_chars=max_chars)
    else:
        raise ValueError(f"Unsupported kind: {kind}")


def _extract_pdf(data: bytes, *, max_chars: int) -> str:
    """Extract text from a PDF, stopping once max_chars is reached.

    We accumulate text page by page and stop as soon as we have enough,
    rather than reading every page and slicing afterwards — a 50,000-page
    PDF must not force us to render every page before we notice we only
    needed the first few thousand characters.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        text_parts = []
        total_len = 0
        for page in reader.pages:
            if total_len >= max_chars:
                break
            text = page.extract_text()
            if text:
                text_parts.append(text)
                total_len += len(text)
        return "".join(text_parts)[:max_chars]
    except MemoryError:
        # Deliberately not caught by the bare `except Exception` below:
        # a corrupt PDF and an exhausted worker are both "we can't read
        # this", but we don't want a broad except to relabel an OOM as a
        # routine parse failure and mask the real cause.
        raise
    except Exception as e:
        # Bare Exception is a deliberate boundary here: pypdf can raise a
        # wide variety of internal error types for malformed input, and we
        # want all of them to surface as UnsupportedDocument rather than
        # leaking library internals to callers.
        raise UnsupportedDocument(f"Failed to parse PDF: {e}") from e


def _extract_docx(data: bytes, *, max_chars: int) -> str:
    """Extract text from a DOCX file, refusing to decompress a bomb.

    The budget is derived from the caller's max_chars — see
    `_DOCX_BOMB_MULTIPLIER` — and is spent across the archive as a whole,
    so a thousand small members cannot do what one large member is
    forbidden to do.

    The actual bounded inflation lives in `app.services.archive`, shared
    with every other zip-based format we read (XLSX included). This
    function's only job on top of that is translating the archive module's
    format-neutral `BoundedArchiveTooLarge` into the `UnsupportedDocument`
    that every caller of this module already expects.
    """
    try:
        budget = max(max_chars * _DOCX_BOMB_MULTIPLIER, _DOCX_MIN_BUDGET_BYTES)
        doc = Document(bounded_archive(data, budget=budget))
        text_parts = []
        total_len = 0
        for para in doc.paragraphs:
            if para.text:
                text_parts.append(para.text)
                total_len += len(para.text) + 1  # +1 for the joining newline
            if total_len >= max_chars:
                break
        return "\n".join(text_parts)[:max_chars]
    except BoundedArchiveTooLarge as e:
        raise UnsupportedDocument(str(e)) from e
    except UnsupportedDocument:
        raise
    except MemoryError:
        # See _extract_pdf: an OOM is not a "bad file" and must not be
        # relabeled as UnsupportedDocument by the broad except below.
        raise
    except Exception as e:
        # Bare Exception is a deliberate boundary here: python-docx and
        # zipfile can raise a variety of internal error types for malformed
        # input, and we want all of them to surface as UnsupportedDocument
        # rather than leaking library internals to callers.
        raise UnsupportedDocument(f"Failed to parse DOCX: {e}") from e
