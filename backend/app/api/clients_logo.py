"""A client's company logo: upload, read, remove.

Mirrors `candidates_avatar.py` closely — the same three rules apply:

1. **The key is computed, never received.** `client_logo_key(tenant_id, ...)`
   is built from the tenant on the session cookie. The client cannot name an
   object, so the `{tenant_id}/` prefix cannot be crossed even though ids are
   visible in URLs.
2. **The client record is resolved through the tenant session.** Another
   agency's id is a 404, not a 403 — see `_load` in `clients.py`, reused here
   rather than reimplemented so the two can never drift apart.
3. **Nothing the caller says about the file is believed.** Not the filename,
   not the Content-Type. The bytes are decoded by Pillow and re-encoded.

The one behavioural difference from the candidate photo path: a logo is
CONTAINED in a square, never cropped to one. A candidate's photo is centred
and cropped to a square because a face survives being cropped; a wordmark
does not. `thumbnail` caps the long edge, then the result is pasted centred
onto a transparent square canvas, so a wide logo keeps every pixel instead of
losing its edges. The stored format is PNG — hardcoded, not a setting,
because the padding needs an alpha channel and JPEG has none.
"""

import io
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import update

from app.api.auth import _require_session
from app.api.clients import _load
from app.core.config import settings
from app.db.rls import tenant_session
from app.models.client import Client
from app.services.storage.r2 import BodyStore, R2BodyStore, client_logo_key

router = APIRouter(tags=["clients"])

_STORED_MIME = "image/png"


def body_store() -> BodyStore:
    """The object store, as a dependency so tests can substitute the double.

    A FastAPI dependency rather than a module global: `app.dependency_overrides`
    is scoped to the test that sets it, whereas a patched global outlives a
    failing test and quietly makes the next one pass against memory.
    """
    return R2BodyStore()


async def _read_within_limit(upload: UploadFile) -> bytes:
    """Read the upload, refusing anything over the configured size.

    The limit is enforced on the way in, before Pillow sees a byte: the
    `Content-Length` header is the client's claim about the body and a
    streaming upload need not send one at all, so the only honest place to
    count is here, as the bytes arrive. Reading one chunk past the limit and
    stopping means a 10 GB post costs the limit plus a chunk, not 10 GB.
    """
    limit = settings.CLIENT_LOGO_MAX_UPLOAD_BYTES
    content = await upload.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"Logo is larger than the {limit} byte limit.",
        )
    if not content:
        raise HTTPException(status_code=400, detail="No logo was uploaded.")
    return content


def _reencode(content: bytes) -> bytes:
    """Decode whatever this really is, and write it back out as a square PNG.

    `Image.open` reads only the header, so the dimensions are known before any
    pixel buffer is allocated — that ordering is the decompression-bomb guard.

    The difference from the candidate path: a logo is CONTAINED, not cropped.
    `thumbnail` caps the long edge in place (and is a no-op for an image
    already within bounds, so nothing is upscaled), then the result is pasted
    into a transparent square. A 400x100 wordmark centre-cropped to a square
    would lose two thirds of the words; letterboxed, it survives.
    """
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.load()  # force the decode here, inside the guard
            bound = settings.CLIENT_LOGO_MAX_PIXEL_DIMENSION
            image.thumbnail((bound, bound))

            # RGBA always: the padding must be transparent, and a palette or
            # CMYK source cannot be pasted onto an RGBA canvas as-is.
            prepared = image if image.mode == "RGBA" else image.convert("RGBA")

            side = max(prepared.width, prepared.height)
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(
                prepared,
                ((side - prepared.width) // 2, (side - prepared.height) // 2),
            )

            buffer = io.BytesIO()
            # PNG is not a setting here, unlike the candidate path. JPEG has
            # no alpha channel, so a format setting that ever moved off PNG
            # would silently turn every logo's padding black.
            #
            # No `exif=` and no `icc_profile=`: saving a fresh image without
            # them IS the strip. Nothing is carried over from the original.
            canvas.save(buffer, format="PNG")
    except Image.DecompressionBombError as exc:
        raise HTTPException(
            status_code=400, detail="Logo dimensions are implausibly large."
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="That file is not a readable image.") from exc
    return buffer.getvalue()


@router.post("/clients/{client_id}/logo")
async def upload_logo(
    request: Request,
    client_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """Replace this client's logo.

    The tenant check happens before the bytes are touched, so an attacker
    guessing another agency's client id cannot even spend our CPU on a
    decode, let alone write an object under their prefix.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        await _load(session, client_id)

    content = await _read_within_limit(file)
    encoded = _reencode(content)

    # Derived from the authenticated tenant, never from anything the client
    # sent. This is the line that makes cross-tenant writes impossible.
    key = client_logo_key(tenant_uuid, client_id)
    await store.put_bytes(key, encoded, _STORED_MIME)

    updated_at = datetime.now(UTC)
    async with tenant_session(tenant_uuid) as session:
        # Re-checked inside the second transaction: the client could have
        # been deleted while the image was being decoded, and the UPDATE is
        # tenant-scoped by RLS regardless.
        await _load(session, client_id)
        await session.execute(
            update(Client)
            .where(Client.id == client_id)
            .values(logo_key=key, logo_updated_at=updated_at)
        )

    return {"logo_key": key, "logo_updated_at": updated_at.isoformat()}


@router.get("/clients/{client_id}/logo")
async def get_logo(
    request: Request,
    client_id: uuid.UUID,
    store: Annotated[BodyStore, Depends(body_store)],
) -> dict:
    """A short-lived URL the browser can load the image from directly.

    Signed per request, after the tenant check, and never stored: a persisted
    URL would be a capability that outlives the permission it was granted
    under, readable by anyone who later saw the row.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        has_logo = client.logo_key is not None

    if not has_logo:
        raise HTTPException(status_code=404, detail="This client has no logo.")

    ttl = settings.CLIENT_LOGO_PRESIGNED_URL_TTL_SECONDS
    # Recomputed rather than read back from the row: the stored value should
    # match, but signing whatever a row happens to hold would turn any future
    # write path into a way to sign an arbitrary key.
    url = await store.presigned_get(client_logo_key(tenant_uuid, client_id), ttl)
    return {"url": url, "expires_in": ttl}


@router.delete("/clients/{client_id}/logo", status_code=204)
async def delete_logo(
    request: Request,
    client_id: uuid.UUID,
    store: Annotated[BodyStore, Depends(body_store)],
) -> Response:
    """Remove the logo: the object first, then the columns.

    That order is deliberate. If the delete fails the columns still name the
    object, so a retry can find it; the reverse leaves an orphan in R2 that no
    row references and no sweep can locate.

    Deleting a client who has no logo succeeds — 204 either way, so a
    double-click is not an error.
    """
    _user_uuid, tenant_uuid = _require_session(request)

    async with tenant_session(tenant_uuid) as session:
        client = await _load(session, client_id)
        has_logo = client.logo_key is not None

    if has_logo:
        await store.delete(client_logo_key(tenant_uuid, client_id))
        async with tenant_session(tenant_uuid) as session:
            await session.execute(
                update(Client)
                .where(Client.id == client_id)
                .values(logo_key=None, logo_updated_at=None)
            )

    return Response(status_code=204)
