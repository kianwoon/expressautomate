"""Bounded zip inflation, and refusing archives that lie about their size."""

import io
import struct
import zipfile

import pytest

from app.services.archive import (
    BoundedArchiveTooLarge,
    archive_contains,
    bounded_archive,
    inflate_bounded,
)


def _forge_uncompressed_size(archive: bytes, member: str, claimed: int) -> bytes:
    """Rewrite a member's declared uncompressed size, in both places a zip
    records it, leaving the compressed stream untouched.

    This is what a malicious zip does: the size fields are metadata written
    by whoever produced the file, and nothing in the format ties them to
    what the DEFLATE stream really yields.
    """
    raw = bytearray(archive)
    name = member.encode()

    # Central directory records: name length at +28, name at +46, size at +24.
    pos = 0
    while (pos := raw.find(b"PK\x01\x02", pos)) >= 0:
        (name_len,) = struct.unpack_from("<H", raw, pos + 28)
        if bytes(raw[pos + 46 : pos + 46 + name_len]) == name:
            struct.pack_into("<I", raw, pos + 24, claimed)
        pos += 4

    # Local headers: name length at +26, name at +30, size at +22.
    pos = 0
    while (pos := raw.find(b"PK\x03\x04", pos)) >= 0:
        (name_len,) = struct.unpack_from("<H", raw, pos + 26)
        if bytes(raw[pos + 30 : pos + 30 + name_len]) == name:
            struct.pack_into("<I", raw, pos + 22, claimed)
        pos += 4

    return bytes(raw)


def test_archive_contains_finds_a_present_member():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
    assert archive_contains(buf.getvalue(), "word/document.xml") is True


def test_archive_contains_is_false_for_an_absent_member():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
    assert archive_contains(buf.getvalue(), "xl/workbook.xml") is False


def test_archive_contains_is_false_for_a_non_zip():
    assert archive_contains(b"just some text", "anything") is False


def test_inflate_bounded_returns_the_real_plaintext_within_budget():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("hello.txt", "hello world")
    data = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("hello.txt")
    assert inflate_bounded(data, info, budget=1000) == b"hello world"


def test_a_member_that_lies_about_its_size_is_still_refused():
    """The bypass a metadata-only guard cannot see.

    The member *declares* 100 bytes, so trusting `ZipInfo.file_size` finds
    nothing wrong — but its DEFLATE stream really expands to 50MB, far past
    the budget. A guard that trusts the declaration would wave this through.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("bomb.bin", b"\x00" * (50 * 1024 * 1024))
    data = _forge_uncompressed_size(buf.getvalue(), "bomb.bin", 100)

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("bomb.bin")
        assert info.file_size == 100  # the forged, innocent-looking claim

    with pytest.raises(BoundedArchiveTooLarge):
        inflate_bounded(data, info, budget=20000 * 100)


def test_bounded_archive_refuses_a_zip_bomb_across_the_whole_archive():
    """A thousand small members cannot do what one large member is
    forbidden to do — the budget is spent across the archive as a whole."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("small.txt", "ordinary content")
        z.writestr("bomb.bin", b"\x00" * (50 * 1024 * 1024))
    data = _forge_uncompressed_size(buf.getvalue(), "bomb.bin", 100)

    with pytest.raises(BoundedArchiveTooLarge):
        bounded_archive(data, budget=20000 * 100)


def test_xlsx_workbook_that_lies_about_its_size_is_refused():
    """The same reverse-bomb shape, but for an XLSX member.

    XLSX is a zip too, so a member named `xl/workbook.xml` that declares a
    small `file_size` but genuinely inflates past the budget must be
    refused by the exact same shared code path DOCX uses — not a second,
    unreviewed copy of it. Built with `zipfile` here rather than committed
    as a binary: the point is the mismatch between claim and reality.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", b"\x00" * (50 * 1024 * 1024))
    data = _forge_uncompressed_size(buf.getvalue(), "xl/workbook.xml", 100)

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert sum(i.file_size for i in z.infolist()) < 20000 * 100

    assert archive_contains(data, "xl/workbook.xml") is True
    with pytest.raises(BoundedArchiveTooLarge):
        bounded_archive(data, budget=20000 * 100)
