"""Extract text from CV files (PDF and DOCX)."""

import io
import zipfile

from docx import Document
from pypdf import PdfReader

# A multiple, not a hardcoded byte count: the caller's max_chars already
# encodes the app's configured limit, and we only need enough headroom to
# admit legitimately verbose documents (rich XML markup around a modest
# amount of text) while still rejecting a zip whose declared size implies
# a decompression bomb. No `settings` import here — this module stays pure
# and the actual limit always comes from the caller.
_DOCX_BOMB_MULTIPLIER = 100


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
            its declared uncompressed size implies a decompression bomb.
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

    `ZipFile.infolist()` reads only the zip's central directory, which
    records each member's declared uncompressed size — it does not
    decompress anything. We sum those sizes and refuse to hand the archive
    to python-docx (which does decompress fully) if the total is wildly
    disproportionate to what we could ever need for max_chars of text.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            declared_size = sum(info.file_size for info in z.infolist())
        if declared_size > max_chars * _DOCX_BOMB_MULTIPLIER:
            raise UnsupportedDocument(
                f"DOCX declared uncompressed size ({declared_size} bytes) is "
                "disproportionate to the requested text limit; refusing to "
                "decompress"
            )

        doc = Document(io.BytesIO(data))
        text_parts = []
        total_len = 0
        for para in doc.paragraphs:
            if para.text:
                text_parts.append(para.text)
                total_len += len(para.text) + 1  # +1 for the joining newline
            if total_len >= max_chars:
                break
        return "\n".join(text_parts)[:max_chars]
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
