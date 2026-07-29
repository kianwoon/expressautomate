"""Bounded, self-verifying zip inflation, shared across every zip-based format.

DOCX and XLSX are both zip containers, and a zip's own metadata about a
member's size is a claim made by whoever wrote the file, not a fact. A
hostile archive can declare one byte and expand to gigabytes when actually
decompressed ("reverse zip bomb"). This module never trusts `ZipInfo`'s
declared sizes: it inflates each member itself, in small chunks, counting
real output against a caller-supplied budget, and stops the instant the
budget is exceeded.

This code moved here unchanged from `app/services/cv/text.py`, which is
where it was first built and hardened over two review rounds (the first
attempt trusted the central directory and a reverse zip bomb walked
straight through it). Any format that needs to read a zip member safely —
DOCX today, XLSX tomorrow — should call these functions rather than
reimplement bounded inflation, because the reimplementation is exactly the
kind of code that doesn't get the same scrutiny twice.
"""

import io
import struct
import zipfile
import zlib

# How much compressed input we hand the decompressor at a time, and how much
# plaintext we let it hand back per call. Both are deliberately small: the
# whole point of the bound is that we get to look at the running total
# between chunks, and a chunk we cannot interrupt is a chunk that can
# exhaust the worker before we ever check.
_INFLATE_CHUNK = 64 * 1024

_STORED = 0
_DEFLATED = 8
_LOCAL_HEADER_SIZE = 30
_FLAG_ENCRYPTED = 0x1


class BoundedArchiveTooLarge(Exception):
    """Raised when a zip member decompresses past the caller's budget.

    Format-neutral on purpose: this module has no notion of DOCX, XLSX, or
    any other document kind. A caller that wants a format-specific error
    (e.g. `UnsupportedDocument`) should catch this and re-raise its own.
    """

    pass


def _member_data_offset(data: bytes, info: zipfile.ZipInfo) -> int:
    """Return the offset of a member's payload, read from its local header.

    The central directory says where the local header starts; the local
    header itself says how long its name and extra fields are. We read those
    two lengths rather than reusing the central directory's copies, because
    the two records can legitimately disagree about extra-field length and
    the local one is what actually precedes the bytes.
    """
    off = info.header_offset
    if data[off : off + 4] != b"PK\x03\x04":
        raise BoundedArchiveTooLarge("archive member has no local file header")
    name_len, extra_len = struct.unpack_from("<HH", data, off + 26)
    return off + _LOCAL_HEADER_SIZE + name_len + extra_len


def inflate_bounded(data: bytes, info: zipfile.ZipInfo, budget: int) -> bytes:
    """Decompress one zip member, giving up the moment it exceeds `budget`.

    This exists because *every* size in a zip file is a claim made by
    whoever wrote the file, not a fact. `ZipInfo.file_size` lives in the
    central directory and a hostile archive is free to declare one byte
    while its DEFLATE stream expands to gigabytes. Any check that reads the
    declaration and then hands the raw bytes to a parser has checked
    nothing — it has only asked the attacker whether they are an attacker.

    So we never consult `file_size` or `compress_size`. We feed the raw
    stream to zlib in chunks, cap how much plaintext zlib may return each
    time, and count what comes back. The instant the running total passes
    the budget we stop and raise, having materialised at most one chunk
    beyond it. This is the defence; deleting it reopens a
    denial-of-service against every tenant sharing this worker.
    """
    if info.flag_bits & _FLAG_ENCRYPTED:
        raise BoundedArchiveTooLarge("archive member is encrypted")

    start = _member_data_offset(data, info)

    if info.compress_type == _STORED:
        # A stored member cannot inflate — its plaintext is already sitting
        # in the archive we were handed, so its size is bounded by the
        # upload limit the caller enforced upstream. We still clamp to the
        # end of the buffer in case the declared length overruns it.
        end = min(len(data), start + max(info.compress_size, 0))
        chunk = data[start:end]
        if len(chunk) > budget:
            raise BoundedArchiveTooLarge(
                "archive member exceeds the size implied by the budget"
            )
        return chunk

    if info.compress_type != _DEFLATED:
        raise BoundedArchiveTooLarge(
            f"archive member uses unsupported compression {info.compress_type}"
        )

    # -15 selects a raw DEFLATE stream (no zlib wrapper), which is what a
    # zip member holds. We read input to the end of the buffer rather than
    # trusting compress_size, and stop on the decompressor's own end-of-
    # stream flag — a declared length that lies in either direction cannot
    # make us read more than the budget allows.
    decompressor = zlib.decompressobj(-15)
    out: list[bytes] = []
    total = 0
    pos = start
    while not decompressor.eof:
        # zlib hands back whatever input it could not fit into max_length
        # bytes of output; that tail must be replayed before we advance
        # through the buffer, or we silently drop compressed data.
        feed = decompressor.unconsumed_tail
        if not feed:
            if pos >= len(data):
                # Input exhausted before the stream ended: truncated member.
                break
            feed = data[pos : pos + _INFLATE_CHUNK]
            pos += len(feed)
        piece = decompressor.decompress(feed, _INFLATE_CHUNK)
        if not piece and not decompressor.unconsumed_tail and pos >= len(data):
            break
        total += len(piece)
        if total > budget:
            raise BoundedArchiveTooLarge(
                "archive decompresses to more than the budget allows; "
                "refusing to continue"
            )
        out.append(piece)
    return b"".join(out)


def bounded_archive(data: bytes, *, budget: int) -> io.BytesIO:
    """Rebuild the archive from bytes we have actually inflated and counted.

    A downstream parser (python-docx, openpyxl, ...) wants a file-like zip,
    and if we handed it the original bytes it would decompress them all
    over again — this time with nothing watching. Rather than reimplementing
    the parts of the package format the downstream library knows how to
    read, we inflate every member ourselves under a shared budget and
    repack the verified plaintext with no compression. What the library
    then opens cannot expand at all: it is already flat, and already known
    to fit.
    """
    remaining = budget
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        with zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_STORED) as out:
            for info in infos:
                if info.is_dir():
                    continue
                # `inflate_bounded` already raises the instant a member's
                # own running total passes `remaining`, so `remaining` can
                # never go negative here — there is no separate check to
                # make once it returns.
                plain = inflate_bounded(data, info, remaining)
                remaining -= len(plain)
                out.writestr(info.filename, plain)
    rebuilt.seek(0)
    return rebuilt


def archive_contains(data: bytes, member: str) -> bool:
    """Check whether a zip contains a named member, without inflating it.

    Used for format sniffing (e.g. does this zip look like a DOCX or an
    XLSX?) where we only need the member's presence, not its content, so
    there is nothing here for the bounded-inflate defence to protect.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return member in z.namelist()
    except (zipfile.BadZipFile, RuntimeError):
        return False
