"""Convert legacy Office documents (.doc) to the .docx the extractor reads.

The text extractor (`text.sniff` + `text.extract_text`) reads the modern Office
formats: a `.docx` is a zip whose `word/document.xml` carries the prose, and
`python-docx` reads it. A legacy `.doc` (Word 97-2003) is a completely different
format — an OLE2 Compound Document, magic bytes `D0CF11E0` — that neither `sniff`
nor `python-docx` can open. Agencies still hold and send these: a CV saved from
an old Word install, exported by a legacy ATS, or attached by a candidate who
never upgraded.

This module is the bridge. When the upload path meets bytes that are clearly an
Office document but not a `.docx`, LibreOffice headless converts them to `.docx`
in a temp dir, and the caller re-runs `sniff` on the result — so the rest of the
pipeline (tables, identity, OCR, the parse) is reused verbatim. Only the OLE2
legacy Word format (`.doc`) actually reaches the converter today: `is_legacy_office`
gates on the OLE2 magic bytes, so RTF and ODT are refused upstream at the upload
route (415) even though LibreOffice itself could convert them with the same
command. Widening the gate is a one-line change here plus the accept list.

Runs entirely inside the worker process — no bytes leave the host — same PDPA
posture as the OCR fallback. Bounded by a timeout, because LibreOffice on a
corrupt or hostile file can hang where nothing of ours is watching.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.services.cv.text import sniff

log = get_logger(__name__)

# The OLE2 Compound Document magic signature — every legacy Office binary file
# (.doc, .xls, .ppt) and a few unrelated formats start with these eight bytes.
# A `.docx` is a zip and starts with `PK`, so this is the unambiguous tell that
# the bytes are NOT the modern format the extractor already handles.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class ConversionUnavailable(Exception):
    """The converter is missing or the run failed to produce a .docx.

    Distinct from a run that completed and produced nothing — that is not a case
    this module reaches, because LibreOffice writes a file or exits non-zero.
    Raised when there is no point retrying: a missing `soffice` binary, a
    timeout, or a corrupt input LibreOffice could not open. The caller surfaces
    the cause as a readable refusal rather than a crash.
    """


def is_legacy_office(data: bytes) -> bool:
    """Whether these bytes are an OLE2 Compound Document (legacy .doc/.xls/.ppt).

    The extractor's `sniff` already rejected them as not-`.docx`-and-not-`.pdf`
    by the time this is called; this answers the follow-up: are they the kind of
    Office document a conversion would rescue? Magic bytes only — never the
    filename, never the Content-Type.
    """
    return data[:8] == _OLE2_MAGIC


def _binary_present() -> bool:
    """`soffice` is the LibreOffice headless entrypoint. macOS cask installs it
    on PATH; Debian's `libreoffice-core` ships `/usr/bin/soffice`."""
    return shutil.which("soffice") is not None


_binary_cache: bool | None = None


def converter_available() -> bool:
    """Whether the LibreOffice headless converter is installed. No subprocess run."""
    global _binary_cache
    if _binary_cache is None:
        _binary_cache = _binary_present()
    return _binary_cache


def _run_soffice_sync(input_path: Path, out_dir: Path) -> Path:
    """The blocking conversion, isolated so `asyncio.to_thread` runs it off the loop.

    `--convert-to docx` writes `<input-stem>.docx` into `out_dir`. The filter
    name is the explicit one LibreOffice's own GUI uses for the OOXML Word
    format, so the output is a real `.docx` zip that `python-docx` reads. The
    HOME env is set under a temp profile so a worker running as `appuser` with a
    read-only home dir does not fail trying to write LibreOffice's config.
    """
    import os

    env = {**os.environ, "HOME": str(out_dir)}
    import subprocess

    result = subprocess.run(
        [
            "soffice",
            "--headless",
            "--norestore",
            "--convert-to",
            "docx:MS Word 2007 XML",
            "--outdir",
            str(out_dir),
            str(input_path),
        ],
        capture_output=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise ConversionUnavailable(
            f"LibreOffice exited {result.returncode}: "
            f"{result.stderr.decode(errors='replace').strip()[:500]}"
        )
    # The output filename is the input stem with a .docx extension. There is
    # exactly one file written for one input; find it rather than guessing the
    # stem, because the input's name may contain characters the filesystem or
    # LibreOffice rewrites.
    docx_files = list(out_dir.glob("*.docx"))
    if not docx_files:
        raise ConversionUnavailable("LibreOffice produced no .docx output.")
    return docx_files[0]


async def convert_to_docx(data: bytes, *, timeout: float) -> bytes:
    """Convert legacy Office bytes to a .docx, returning the .docx bytes.

    Raises `ConversionUnavailable` for a missing converter, a timeout, or a
    corrupt input. The caller sniffs the returned bytes to confirm they are a
    real `.docx` before trusting them — defence in depth, in case LibreOffice
    wrote something it labelled `.docx` but is not the zip the extractor expects.

    Runs in a worker thread (`asyncio.to_thread`) so LibreOffice — a full office
    suite doing a real document load — does not block the arq event loop, and
    bounded by `timeout` so a stuck conversion cannot hold a worker slot.
    """
    if not converter_available():
        raise ConversionUnavailable(
            "Legacy document conversion is enabled but LibreOffice is not installed."
        )

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        input_path = td_path / "input.doc"
        input_path.write_bytes(data)
        try:
            docx_path = await asyncio.wait_for(
                asyncio.to_thread(_run_soffice_sync, input_path, td_path),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise ConversionUnavailable(
                f"Document conversion did not finish within {timeout:.0f}s."
            ) from exc
        return docx_path.read_bytes()


async def maybe_convert(data: bytes, *, kind: str | None) -> tuple[bytes, str]:
    """The bytes and their sniffed kind, converting legacy Office if needed.

    The common case is a no-op: `kind` is already `"pdf"` or `"docx"` and the
    bytes pass through unchanged. Only when `kind is None` AND the bytes are an
    OLE2 legacy Office document does conversion run. On success the returned
    kind is the freshly-sniffed `"docx"` of the converted bytes.

    Raises `ConversionUnavailable` when conversion is enabled but fails, so the
    caller can surface a named cause. When conversion is not enabled and the
    bytes are legacy Office, returns `(data, None)` — the caller's existing
    "neither PDF nor Word" refusal applies, and the recruiter sees the honest
    "save as .docx" message rather than a silent nothing.
    """
    if kind is not None or not is_legacy_office(data):
        return data, kind
    if not settings.conversion_configured():
        return data, None
    converted = await convert_to_docx(data, timeout=settings.CV_CONVERT_TIMEOUT_SECONDS)
    return converted, sniff(converted)
