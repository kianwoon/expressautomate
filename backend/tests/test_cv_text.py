"""Turning an uploaded file into text, and refusing what we cannot read."""

import io
import zipfile

import pytest
from pypdf import PdfWriter

from app.services.cv.text import UnsupportedDocument, extract_text, sniff


def test_a_pdf_is_recognised_by_its_bytes():
    assert sniff(b"%PDF-1.7\nrest of file") == "pdf"


def test_a_bare_zip_is_not_a_docx():
    """A DOCX is a zip, so `PK` alone proves nothing.

    Accepting any archive means a recruiter can hand us a zip of holiday
    photos and get an unreadable job instead of a straight refusal.
    """
    assert sniff(b"PK\x03\x04" + b"\x00" * 64) is None


def test_a_real_docx_is_recognised(tmp_path):
    """Built here rather than committed as a fixture binary: the point is the
    presence of `word/document.xml` inside the archive, and that is clearer
    written than checked in."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
    assert sniff(buf.getvalue()) == "docx"


def test_something_that_is_neither_is_refused():
    assert sniff(b"just some text") is None


def test_pdf_with_no_extractable_text_returns_empty_string():
    """A scan or image-only PDF has no text to extract, so we return empty."""
    buf = io.BytesIO()
    writer = PdfWriter()
    # Create a blank page with no text
    writer.add_blank_page(width=612, height=792)
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    assert sniff(pdf_bytes) == "pdf"
    assert extract_text(pdf_bytes, "pdf", max_chars=1000) == ""


def test_corrupt_pdf_raises_unsupported_document():
    """A malformed PDF that cannot be parsed raises UnsupportedDocument."""
    corrupt_pdf = b"%PDF-1.4\n" + b"corrupted content" * 100

    assert sniff(corrupt_pdf) == "pdf"
    with pytest.raises(UnsupportedDocument):
        extract_text(corrupt_pdf, "pdf", max_chars=1000)


def test_docx_with_disproportionate_declared_size_is_refused():
    """A DOCX whose zip directory claims a wildly disproportionate
    uncompressed size is refused before python-docx ever decompresses it.

    We build this with plain zipfile rather than committing a binary bomb:
    a single member can *declare* any file_size in its header regardless of
    what bytes actually follow, which is exactly the property a bomb
    detector must catch without inflating the payload.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        # A real document.xml so sniff() still recognises this as a docx.
        z.writestr("word/document.xml", "<w:document/>")
        # Forge a directory entry claiming an enormous uncompressed size,
        # without writing that many bytes into the archive.
        info = zipfile.ZipInfo("word/media/huge.bin")
        z.writestr(info, b"x" * 10)
        # Patch the central-directory record after the fact so file_size
        # looks like a decompression bomb, mirroring what a hand-crafted
        # malicious zip would declare.
        for zinfo in z.infolist():
            if zinfo.filename == "word/media/huge.bin":
                zinfo.file_size = 200 * 1024 * 1024

    docx_bytes = buf.getvalue()
    assert sniff(docx_bytes) == "docx"
    with pytest.raises(UnsupportedDocument):
        extract_text(docx_bytes, "docx", max_chars=20000)


def test_docx_text_is_truncated_to_max_chars():
    """Extracted DOCX text never exceeds the caller's max_chars bound.

    Built with python-docx itself (rather than hand-rolled XML) so the
    relationships/content-types parts are all valid and the only thing
    under test is our truncation, not docx-format minutiae.
    """
    from docx import Document as DocxDocument

    doc = DocxDocument()
    long_paragraph = "word " * 500
    for _ in range(200):
        doc.add_paragraph(long_paragraph)
    buf = io.BytesIO()
    doc.save(buf)
    docx_bytes = buf.getvalue()

    # A stock python-docx document already declares ~800KB of styles/theme
    # XML regardless of body content, so max_chars must be large enough
    # (as a real CV-text limit would be) that this legitimate overhead
    # doesn't itself trip the bomb check.
    assert sniff(docx_bytes) == "docx"
    full_length = len(long_paragraph) * 200
    result = extract_text(docx_bytes, "docx", max_chars=20000)
    assert len(result) <= 20000
    assert len(result) < full_length


def test_pdf_extraction_stops_early_rather_than_reading_every_page():
    """A PDF with many pages stops accumulating once max_chars is reached,
    so returned length reflects the bound, not the full document."""
    buf = io.BytesIO()
    writer = PdfWriter()
    for _ in range(50):
        writer.add_blank_page(width=612, height=792)
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    assert sniff(pdf_bytes) == "pdf"
    result = extract_text(pdf_bytes, "pdf", max_chars=10)
    assert len(result) <= 10
