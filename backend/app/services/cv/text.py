"""Extract text from CV files (PDF and DOCX)."""

import io
import zipfile

from docx import Document
from pypdf import PdfReader


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


def extract_text(data: bytes, kind: str) -> str:
    """Extract text from a file of the given kind.

    A PDF or DOCX with no extractable text (e.g. a scan) returns "".
    Corrupt files raise UnsupportedDocument rather than leaking library errors.

    Args:
        data: File bytes.
        kind: One of "pdf" or "docx".

    Returns:
        Extracted text, possibly empty.

    Raises:
        UnsupportedDocument: If the file cannot be parsed.
    """
    if kind == "pdf":
        return _extract_pdf(data)
    elif kind == "docx":
        return _extract_docx(data)
    else:
        raise ValueError(f"Unsupported kind: {kind}")


def _extract_pdf(data: bytes) -> str:
    """Extract text from a PDF."""
    try:
        reader = PdfReader(io.BytesIO(data))
        text_parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
        return "".join(text_parts)
    except Exception as e:
        raise UnsupportedDocument(f"Failed to parse PDF: {e}") from e


def _extract_docx(data: bytes) -> str:
    """Extract text from a DOCX file."""
    try:
        doc = Document(io.BytesIO(data))
        text_parts = []
        for para in doc.paragraphs:
            if para.text:
                text_parts.append(para.text)
        return "\n".join(text_parts)
    except Exception as e:
        raise UnsupportedDocument(f"Failed to parse DOCX: {e}") from e
