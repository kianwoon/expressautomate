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
    assert extract_text(pdf_bytes, "pdf") == ""


def test_corrupt_pdf_raises_unsupported_document():
    """A malformed PDF that cannot be parsed raises UnsupportedDocument."""
    corrupt_pdf = b"%PDF-1.4\n" + b"corrupted content" * 100

    assert sniff(corrupt_pdf) == "pdf"
    with pytest.raises(UnsupportedDocument):
        extract_text(corrupt_pdf, "pdf")
