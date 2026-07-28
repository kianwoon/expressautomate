"""Extract text from CV files (PDF and DOCX)."""

import io
import struct
import zipfile
import zlib

from docx import Document
from pypdf import PdfReader

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
        raise UnsupportedDocument("DOCX member has no local file header")
    name_len, extra_len = struct.unpack_from("<HH", data, off + 26)
    return off + _LOCAL_HEADER_SIZE + name_len + extra_len


def _inflate_bounded(data: bytes, info: zipfile.ZipInfo, budget: int) -> bytes:
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
        raise UnsupportedDocument("DOCX member is encrypted")

    start = _member_data_offset(data, info)

    if info.compress_type == _STORED:
        # A stored member cannot inflate — its plaintext is already sitting
        # in the archive we were handed, so its size is bounded by the
        # upload limit the caller enforced upstream. We still clamp to the
        # end of the buffer in case the declared length overruns it.
        end = min(len(data), start + max(info.compress_size, 0))
        chunk = data[start:end]
        if len(chunk) > budget:
            raise UnsupportedDocument(
                "DOCX member exceeds the size implied by the text limit"
            )
        return chunk

    if info.compress_type != _DEFLATED:
        raise UnsupportedDocument(
            f"DOCX member uses unsupported compression {info.compress_type}"
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
            raise UnsupportedDocument(
                "DOCX decompresses to more than the size implied by the "
                "text limit; refusing to continue"
            )
        out.append(piece)
    return b"".join(out)


def _bounded_docx_archive(data: bytes, *, budget: int) -> io.BytesIO:
    """Rebuild the archive from bytes we have actually inflated and counted.

    python-docx wants a file-like zip, and if we handed it the original
    bytes it would decompress them all over again — this time with nothing
    watching. Rather than reimplementing the parts of the OOXML package
    python-docx knows how to read, we inflate every member ourselves under
    a shared budget and repack the verified plaintext with no compression.
    What python-docx then opens cannot expand at all: it is already flat,
    and already known to fit.
    """
    remaining = budget
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        with zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_STORED) as out:
            for info in infos:
                if info.is_dir():
                    continue
                # `_inflate_bounded` already raises the instant a member's
                # own running total passes `remaining`, so `remaining` can
                # never go negative here — there is no separate check to
                # make once it returns.
                plain = _inflate_bounded(data, info, remaining)
                remaining -= len(plain)
                out.writestr(info.filename, plain)
    rebuilt.seek(0)
    return rebuilt


def _extract_docx(data: bytes, *, max_chars: int) -> str:
    """Extract text from a DOCX file, refusing to decompress a bomb.

    The budget is derived from the caller's max_chars — see
    `_DOCX_BOMB_MULTIPLIER` — and is spent across the archive as a whole,
    so a thousand small members cannot do what one large member is
    forbidden to do.
    """
    try:
        budget = max(max_chars * _DOCX_BOMB_MULTIPLIER, _DOCX_MIN_BUDGET_BYTES)
        doc = Document(_bounded_docx_archive(data, budget=budget))
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
