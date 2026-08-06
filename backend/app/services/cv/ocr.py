"""OCR fallback for scanned PDFs that yield no text layer.

The ordinary PDF text extractor (`text._extract_pdf`) reads the text layer a
digital PDF carries. A scanned or photographed CV has no text layer — only a
picture of one — so it returns "" and the parse marks the document `unreadable`.
This module is the fallback for exactly that case: it runs Tesseract (via the
`ocrmypdf` orchestrator) on the bytes and returns the text it recovers, so a
scanned CV flows through the same parse pipeline as a digital one.

Runs entirely inside the worker process — no bytes leave the host — which is the
reason it is the first-choice OCR engine for a platform holding candidate PII
under Singapore's PDPA. A cloud doc-AI fallback is the documented escape hatch
for the cases Tesseract cannot read, but it is not built yet; this is measured
against real CVs first.

Bound like the text extractors, for the same reasons: `max_pages` stops a
hostile 50,000-page PDF at a CV-sized ceiling, and `timeout` stops a stuck
Ghostscript from holding a worker slot past the parse job's own budget. The
caller supplies both from configuration; this module enforces whatever it is
given and hardcodes neither.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import ocrmypdf

from app.core.logging import get_logger

log = get_logger(__name__)


class OCRUnavailable(Exception):
    """The OCR toolchain is missing or the run failed to start.

    Distinct from an OCR run that completed and yielded no text — that is the
    honest empty-string answer, returned rather than raised. This is raised
    when there is no point retrying on this document: a missing binary, an
    encrypted PDF, or a toolchain the deployment did not install. The caller
    marks the document `unreadable` with a sentence naming the cause.
    """


def _binaries_present() -> bool:
    """Tesseract is the load-bearing binary; ocrmypdf checks its own deps at call.

    Ghostscript and QPDF are checked too because `ocrmypdf` needs both at runtime
    and a partial install (Tesseract present, one of the others stripped) would
    otherwise fail mid-run rather than at the availability gate.
    """
    return all(shutil.which(name) for name in ("tesseract", "gs", "qpdf"))


# Module-level so `ocr_configured()` can read it without a subprocess, and so a
# test can flip it to exercise the "binary missing" branch without patching
# `shutil.which`. `None` means "not yet probed"; the probe is cheap but there is
# no reason to repeat it.
_binary_cache: bool | None = None


def ocr_available() -> bool:
    """Whether the OCR toolchain is installed and invocable. No subprocess run."""
    global _binary_cache
    if _binary_cache is None:
        _binary_cache = _binaries_present()
    return _binary_cache


def _page_range(max_pages: int) -> str:
    """The `pages=` argument ocrmypdf expects: a 1-indexed inclusive range.

    `ocrmypdf` accepts `"N-M"` and applies it before any work, so a hostile PDF
    is bounded at the source rather than after Tesseract has already ground
    through ten thousand pages. `max_pages` is the caller's configured ceiling.
    """
    return f"1-{max_pages}"


def _run_ocr_sync(
    input_path: Path, output_path: Path, sidecar_path: Path, *, languages: str, max_pages: int
) -> None:
    """The blocking call, isolated so `asyncio.to_thread` can run it off the loop.

    `force_ocr` (not `skip_text`) because the only caller is the empty-text
    branch: the PDF has no text layer, so every page is OCR'd regardless. The
    two flags are mutually exclusive in ocrmypdf, and `force_ocr` is the one
    that matches the scanned-PDF case.

    `sidecar` writes the recovered text straight to a file, which is one fewer
    PDF-parse than reading text back out of the OCR'd output. `deskew` and
    `rotate_pages` handle a photographed CV held at a slight angle — the common
    case for a recruiter who snapped a paper CV on a desk.
    """
    ocrmypdf.ocr(
        str(input_path),
        str(output_path),
        language=languages,
        force_ocr=True,
        deskew=True,
        rotate_pages=True,
        pages=_page_range(max_pages),
        sidecar=str(sidecar_path),
    )


async def ocr_text(
    data: bytes,
    *,
    languages: str,
    max_pages: int,
    timeout: float,
) -> str:
    """OCR a PDF's bytes and return the recovered text, possibly empty.

    Empty is a real answer — a photograph of a blank page, or a scan too low-DPI
    for Tesseract to read — and is returned rather than raised. The caller
    decides what an empty OCR result means (terminal `unreadable`).

    Raises `OCRUnavailable` for a missing toolchain or an unrecoverable run
    (encrypted PDF, corrupt input), so the caller can surface the cause rather
    than retry a document that will fail the same way forever.

    Runs in a worker thread (`asyncio.to_thread`) so a long Tesseract call does
    not block the arq event loop, and bounded by `timeout` so a stuck Ghostscript
    cannot hold a worker slot past the parse job's own budget.
    """
    if not ocr_available():
        raise OCRUnavailable(
            "OCR is enabled but the Tesseract/Ghostscript/QPDF toolchain is not installed."
        )

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        input_path = td_path / "input.pdf"
        output_path = td_path / "output.pdf"
        sidecar_path = td_path / "text.txt"
        input_path.write_bytes(data)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    _run_ocr_sync,
                    input_path,
                    output_path,
                    sidecar_path,
                    languages=languages,
                    max_pages=max_pages,
                ),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise OCRUnavailable(
                f"OCR did not finish within {timeout:.0f}s."
            ) from exc
        except ocrmypdf.MissingDependencyError as exc:
            raise OCRUnavailable(f"An OCR dependency is missing: {exc}") from exc
        except ocrmypdf.EncryptedPdfError as exc:
            raise OCRUnavailable("This PDF is encrypted; OCR cannot read it.") from exc
        except ocrmypdf.ExitCodeException as exc:
            # Any other non-zero exit: a corrupt PDF, an image format Tesseract
            # rejects, an out-of-memory. These are not retried — the same bytes
            # will fail the same way — so they surface as a named cause rather
            # than the library's raw stderr.
            raise OCRUnavailable(f"OCR failed (exit {exc.exitcode}): {exc}") from exc

        if not sidecar_path.exists():
            return ""
        return sidecar_path.read_text(encoding="utf-8", errors="replace")
