"""A candidate's photo: upload, read, remove.

Separate from `candidates.py` because that file is already near the size
ceiling, and because none of this is really about candidate *records* — it is
about bytes that arrive from a browser and must be treated as hostile until
proven otherwise.

Three rules shape everything here:

1. **The key is computed, never received.** `avatar_key(tenant_id, ...)` is
   built from the tenant on the session cookie. The client cannot name an
   object, so the `{tenant_id}/` prefix cannot be crossed even though ids are
   visible in URLs.
2. **The candidate is resolved through the tenant session.** Another agency's
   id is a 404, not a 403 — see `_load` in `candidates.py`, reused here rather
   than reimplemented so the two can never drift apart.
3. **Nothing the client says about the file is believed.** Not the filename,
   not the Content-Type. The bytes are decoded by Pillow and re-encoded, and
   that re-encode is what strips EXIF: a phone photo carries GPS coordinates
   of the candidate's home, and passing the original bytes through would
   publish them behind a URL.
"""

import io
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import update

from app.api.auth import _require_session
from app.api.candidates import _load
from app.core.config import settings
from app.db.rls import tenant_session
from app.models.candidate import Candidate
from app.services.storage.r2 import BodyStore, R2BodyStore, avatar_key

router = APIRouter(tags=["candidates"])

# Which Pillow plugins `Image.open` is allowed to try. A module constant, not a
# setting: this is a security allowlist and must not be reachable from .env,
# where widening it would be a config change rather than a code review. Left
# unrestricted, Pillow will try every registered plugin — including EPS, which
# shells out to ghostscript when it is installed, a historical RCE surface.
# Nothing about a candidate photo needs it.
_ALLOWED_FORMATS = ["PNG", "JPEG", "GIF", "WEBP", "BMP", "ICO"]


def body_store() -> BodyStore:
    """The object store, as a dependency so tests can substitute the double.

    A FastAPI dependency rather than a module global: `app.dependency_overrides`
    is scoped to the test that sets it, whereas a patched global outlives a
    failing test and quietly makes the next one pass against memory.
    """
    return R2BodyStore()


def _stored_mime() -> str:
    """MIME type for `AVATAR_STORED_FORMAT`, from Pillow's own registry.

    Looked up rather than written down beside the format name: the two would
    drift the first time the format setting changed, and R2 would then serve
    every avatar under a Content-Type that lies about its bytes.
    """
    Image.init()  # populates Image.MIME; plugins are registered lazily
    mime = Image.MIME.get(settings.AVATAR_STORED_FORMAT.upper())
    if mime is None:  # pragma: no cover - a misconfigured deployment
        raise HTTPException(
            status_code=500,
            detail=(
                f"AVATAR_STORED_FORMAT {settings.AVATAR_STORED_FORMAT!r} "
                "is not a Pillow format."
            ),
        )
    return mime


async def _read_within_limit(upload: UploadFile) -> bytes:
    """Read the upload, refusing anything over the configured size.

    The limit is enforced on the way in, before Pillow sees a byte: the
    `Content-Length` header is the client's claim about the body and a
    streaming upload need not send one at all, so the only honest place to
    count is here, as the bytes arrive. Reading one chunk past the limit and
    stopping means a 10 GB post costs the limit plus a chunk, not 10 GB.
    """
    limit = settings.AVATAR_MAX_UPLOAD_BYTES
    content = await upload.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"Photo is larger than the {limit} byte limit.",
        )
    if not content:
        raise HTTPException(status_code=400, detail="No photo was uploaded.")
    return content


def _reencode(content: bytes) -> bytes:
    """Decode whatever this really is, and write it back out clean.

    `Image.open` reads only the header, so the dimensions are known before any
    pixel buffer is allocated — that ordering is the decompression-bomb guard.
    Pillow raises `DecompressionBombError` from `open` for an absurd declared
    canvas, and both that and an unreadable file must land as a 4xx: a caller
    posting a PDF is a bad request, not a server fault.

    `thumbnail` caps the long edge in place and is a no-op for an image already
    within bounds, so a small photo is not upscaled.
    """
    try:
        with Image.open(io.BytesIO(content), formats=_ALLOWED_FORMATS) as image:
            # Pillow's own bomb check only fires above ~179 Mpx, and a decode
            # far below that still allocates hundreds of megabytes. The header
            # has been read here but no pixel buffer exists yet, so this is the
            # one window in which the declared size can be refused for free.
            width, height = image.size
            if width * height > settings.IMAGE_DECODE_MAX_PIXELS:
                raise HTTPException(
                    status_code=400, detail="Photo dimensions are implausibly large."
                )
            image.load()  # force the decode here, inside the guard
            bound = settings.AVATAR_MAX_PIXEL_DIMENSION
            image.thumbnail((bound, bound))

            # Palette, CMYK and 16-bit modes cannot be written to every target
            # format. Normalising to RGBA keeps transparency where it exists
            # and is accepted everywhere Pillow can save.
            prepared = image if image.mode in {"RGB", "RGBA"} else image.convert("RGBA")

            buffer = io.BytesIO()
            # No `exif=` and no `icc_profile=`: saving a fresh image without
            # them IS the strip. Nothing is copied over from the original.
            prepared.save(buffer, format=settings.AVATAR_STORED_FORMAT.upper())
    except Image.DecompressionBombError as exc:
        raise HTTPException(
            status_code=400, detail="Photo dimensions are implausibly large."
        ) from exc
    except UnidentifiedImageError as exc:
        accepted = ", ".join(_ALLOWED_FORMATS[:-1]) + " or " + _ALLOWED_FORMATS[-1]
        raise HTTPException(
            status_code=400, detail=f"We accept {accepted} images."
        ) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="That file is not a readable image.") from exc
    return buffer.getvalue()


@router.post("/candidates/{candidate_id}/avatar")
async def upload_avatar(
    request: Request,
    candidate_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """Replace this candidate's photo.

    The tenant check happens before the bytes are touched, so an attacker
    guessing another agency's candidate id cannot even spend our CPU on a
    decode, let alone write an object under their prefix.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        await _load(session, candidate_id)

    content = await _read_within_limit(file)
    encoded = _reencode(content)

    # Derived from the authenticated tenant, never from anything the client
    # sent. This is the line that makes cross-tenant writes impossible.
    key = avatar_key(tenant_uuid, candidate_id)
    await store.put_bytes(key, encoded, _stored_mime())

    updated_at = datetime.now(UTC)
    async with tenant_session(tenant_uuid) as session:
        # Re-checked inside the second transaction: the candidate could have
        # been deleted while the image was being decoded, and the UPDATE is
        # tenant-scoped by RLS regardless.
        await _load(session, candidate_id)
        await session.execute(
            update(Candidate)
            .where(Candidate.id == candidate_id)
            .values(avatar_key=key, avatar_updated_at=updated_at)
        )

    return {"avatar_key": key, "avatar_updated_at": updated_at.isoformat()}


@router.get("/candidates/{candidate_id}/avatar")
async def get_avatar(
    request: Request,
    candidate_id: uuid.UUID,
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """A short-lived URL the browser can load the image from directly.

    Signed per request, after the tenant check, and never stored: a persisted
    URL would be a capability that outlives the permission it was granted
    under, readable by anyone who later saw the row.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        has_avatar = candidate.avatar_key is not None

    if not has_avatar:
        raise HTTPException(status_code=404, detail="This candidate has no photo.")

    ttl = settings.AVATAR_PRESIGNED_URL_TTL_SECONDS
    # Recomputed rather than read back from the row: the stored value should
    # match, but signing whatever a row happens to hold would turn any future
    # write path into a way to sign an arbitrary key.
    url = await store.presigned_get(avatar_key(tenant_uuid, candidate_id), ttl)
    return {"url": url, "expires_in": ttl}


@router.delete("/candidates/{candidate_id}/avatar", status_code=204)
async def delete_avatar(
    request: Request,
    candidate_id: uuid.UUID,
    store: Annotated[BodyStore, Depends(body_store)],
) -> Response:
    """Remove the photo: the object first, then the columns.

    That order is deliberate. If the delete fails the columns still name the
    object, so a retry can find it; the reverse leaves an orphan in R2 that no
    row references and no sweep can locate.

    Deleting a candidate who has no photo succeeds — 204 either way, so a
    double-click is not an error.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        candidate = await _load(session, candidate_id)
        has_avatar = candidate.avatar_key is not None

    if has_avatar:
        await store.delete(avatar_key(tenant_uuid, candidate_id))
        async with tenant_session(tenant_uuid) as session:
            await session.execute(
                update(Candidate)
                .where(Candidate.id == candidate_id)
                .values(avatar_key=None, avatar_updated_at=None)
            )

    return Response(status_code=204)
