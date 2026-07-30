"""Client logos: what may be stored, who may see it, and what shape it keeps.

The upload path is a near-mirror of `test_candidate_avatar.py`. The one
behavioural difference under test here: a logo is CONTAINED in a square, not
cropped to one — a wide wordmark must survive with transparent padding rather
than losing its edges.
"""

import io
import math
import struct
import uuid
import zlib

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageFile
from sqlalchemy import text

from app.api import clients_logo
from app.core.config import settings
from app.main import app
from app.services.storage.r2 import InMemoryBodyStore, client_logo_key
from tests.conftest import AdminSessionLocal, cleanup_tenant
from tests.test_opportunities_api import sign_in


def test_client_logo_key_is_tenant_prefixed():
    """The `{tenant_id}/` prefix is what a tenant purge sweeps by. A key
    without it survives an erasure request."""
    tenant = uuid.UUID("11111111-1111-1111-1111-111111111111")
    client = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert client_logo_key(tenant, client) == f"{tenant}/clients/{client}/logo"


@pytest.fixture
async def agency_with_clients():
    tid, uid = uuid.uuid4(), uuid.uuid4()
    ids = {"live": uuid.uuid4()}
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'owner')"
            ),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, "
                "email_domain, status) VALUES (:i, :t, 'Acme', 'acme', 'acme.com', 'unconfirmed')"
            ),
            {"i": ids["live"], "t": tid},
        )
        await s.commit()
    yield tid, uid, ids
    await cleanup_tenant(tid)


@pytest.fixture
async def other_agency():
    """A second agency, so "not yours" can be told apart from "not there"."""
    tid, uid = uuid.uuid4(), uuid.uuid4()
    cid = uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, role) "
                "VALUES (:i, :t, :e, 'owner')"
            ),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO clients (id, tenant_id, name, name_normalized, "
                "email_domain, status) "
                "VALUES (:i, :t, 'Other', 'other', 'other.com', 'unconfirmed')"
            ),
            {"i": cid, "t": tid},
        )
        await s.commit()
    yield tid, uid, cid
    await cleanup_tenant(tid)


@pytest.fixture
async def store():
    """The object store, swapped for the in-memory double.

    Overridden through `app.dependency_overrides` rather than by patching a
    module global: the override is undone here even when a test fails, so a
    failure can never leave the next test writing to real R2.
    """
    double = InMemoryBodyStore()
    app.dependency_overrides[clients_logo.body_store] = lambda: double
    yield double
    app.dependency_overrides.pop(clients_logo.body_store, None)


def _client_for(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(http, uid, tid)
    return http


def _png_bytes(w: int = 64, h: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_with_exif() -> bytes:
    """A JPEG carrying EXIF, so re-encoding-as-the-strip can be verified."""
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMake"  # Make
    exif[0x0110] = "SecretCameraModel"  # Model
    exif[0x8825] = {1: "N", 2: (1.0, 23.0, 0.0)}  # GPS IFD
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), (200, 30, 30)).save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


async def _upload(http: AsyncClient, cid: uuid.UUID, content: bytes, **kwargs):
    return await http.post(
        f"/api/clients/{cid}/logo",
        files={"file": (kwargs.get("name", "logo.png"), content, kwargs.get("type", "image/png"))},
    )


async def test_upload_then_get_then_delete(agency_with_clients, store):
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with _client_for(tid, uid) as http:
        uploaded = await _upload(http, target, _png_bytes(300, 100))
        assert uploaded.status_code == 200
        assert uploaded.json()["logo_key"].startswith(str(tid))

        presigned = await http.get(f"/api/clients/{target}/logo")
        assert presigned.status_code == 200
        assert presigned.json()["expires_in"] == settings.CLIENT_LOGO_PRESIGNED_URL_TTL_SECONDS

        assert (await http.delete(f"/api/clients/{target}/logo")).status_code == 204
        assert (await http.get(f"/api/clients/{target}/logo")).status_code == 404


async def test_a_wide_logo_is_contained_not_cropped(agency_with_clients, store):
    """The rule that distinguishes a logo from a candidate photo.

    A 400x100 wordmark centre-cropped to a square loses two thirds of the
    words. Contained, it survives with transparent padding above and below.
    """
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with _client_for(tid, uid) as http:
        await _upload(http, target, _png_bytes(400, 100), name="wide.png")

    stored, _content_type = store.binary_objects[client_logo_key(tid, target)]
    image = Image.open(io.BytesIO(stored))
    assert image.width == image.height  # square canvas
    assert image.mode == "RGBA"  # alpha, so the padding is transparent
    assert image.format == "PNG"

    # The original content is intact and centred, not cropped: the padding
    # rows at the very top are fully transparent and the middle band is not.
    assert image.getpixel((image.width // 2, 2))[3] == 0
    assert image.getpixel((image.width // 2, image.height // 2))[3] != 0


async def test_an_oversized_logo_is_capped_and_stays_wide(agency_with_clients, store):
    """The untested branch: `thumbnail` is a no-op below the bound, so every
    other test in this file leaves the actual resize path unexercised.

    A 2000x500 source (4:1) must come back with its long edge capped at
    exactly the configured bound, still letterboxed onto a square, and with
    its aspect ratio preserved — not squashed to 1:1.
    """
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    bound = settings.CLIENT_LOGO_MAX_PIXEL_DIMENSION
    async with _client_for(tid, uid) as http:
        await _upload(http, target, _png_bytes(2000, 500), name="huge.png")

    stored, _content_type = store.binary_objects[client_logo_key(tid, target)]
    image = Image.open(io.BytesIO(stored))
    assert image.width == image.height == bound  # capped on the long edge

    # 2000x500 is 4:1; scaled so the long edge is exactly `bound`, the short
    # edge should be bound / 4 (allow a pixel of rounding either way).
    expected_band_height = round(bound * (500 / 2000))

    # Scan the vertical opaque extent down the centre column: the band is
    # the run of non-transparent rows, the rest is transparent padding.
    centre_x = image.width // 2
    opaque_rows = [
        y for y in range(image.height) if image.getpixel((centre_x, y))[3] != 0
    ]
    band_height = max(opaque_rows) - min(opaque_rows) + 1
    assert abs(band_height - expected_band_height) <= 1

    # Padding survives above and below the band.
    assert image.getpixel((centre_x, 0))[3] == 0
    assert image.getpixel((centre_x, image.height - 1))[3] == 0


async def test_a_small_logo_is_not_upscaled(agency_with_clients, store):
    """The other half of the untested branch: `thumbnail` must be a genuine
    no-op below the bound, not merely "small enough to look right" —
    confirm the content stays at its original 64x64, not stretched up to
    fill the canvas."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with _client_for(tid, uid) as http:
        await _upload(http, target, _png_bytes(64, 64), name="small.png")

    stored, _content_type = store.binary_objects[client_logo_key(tid, target)]
    image = Image.open(io.BytesIO(stored))
    assert image.width == image.height  # square canvas (64x64 is already square)

    centre_x = image.width // 2
    opaque_rows = [
        y for y in range(image.height) if image.getpixel((centre_x, y))[3] != 0
    ]
    band_height = max(opaque_rows) - min(opaque_rows) + 1
    assert band_height == 64

    opaque_cols = [
        x for x in range(image.width) if image.getpixel((x, image.height // 2))[3] != 0
    ]
    band_width = max(opaque_cols) - min(opaque_cols) + 1
    assert band_width == 64


async def test_oversized_upload_is_413_before_decode(agency_with_clients, store):
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    too_big = b"x" * (settings.CLIENT_LOGO_MAX_UPLOAD_BYTES + 1024)
    async with _client_for(tid, uid) as http:
        # 413, not 400: it was never decoded, so "not a readable image" would
        # be the wrong answer even though these bytes are not one.
        response = await _upload(http, target, too_big)
    assert response.status_code == 413


async def test_a_non_image_is_400_not_500(agency_with_clients, store):
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with _client_for(tid, uid) as http:
        response = await _upload(http, target, b"%PDF-1.4 not an image", name="cv.pdf")
    assert response.status_code == 400


def _png_header_only(width: int, height: int) -> bytes:
    """A structurally valid PNG whose IHDR lies about an enormous canvas.

    Under a hundred bytes on the wire, because the pixel data is a few
    compressed zeros. `Image.open` parses the IHDR and reports the declared
    size; anything that goes on to `load()` allocates width*height*channels.
    Built by hand rather than by Pillow: asking Pillow for a real image this
    large is exactly the allocation the guard exists to prevent.
    """

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit truecolour
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\0" * 16))
        + chunk(b"IEND", b"")
    )


async def test_a_canvas_over_the_decode_budget_is_refused_without_allocating(
    agency_with_clients, store, monkeypatch
):
    """The gap Pillow's own bomb check leaves open — and proof of ordering.

    `DecompressionBombError` only fires above ~179 Mpx. A canvas below that but
    above our budget used to sail through `open` and allocate hundreds of
    megabytes in `load()` — enough to OOM the container, from any signed-in
    session, repeatedly.

    That the payload is a handful of bytes is the point: if the refusal
    happened after decoding, this test would allocate gigabytes rather than
    return.

    The 400/empty-store assertions alone do not prove the check runs before
    the decode: if the size guard were moved to after `image.load()`, Pillow
    would allocate the buffer, then the truncated IDAT would raise `OSError`,
    which the endpoint already maps to the same 400 with nothing stored — the
    test would stay green either way. To make the ordering itself the thing
    under test, `ImageFile.load` is monkeypatched to raise `AssertionError` if
    it is ever called. If the guard still runs first, `load` is never reached
    and the request returns its ordinary 400. If the guard ever moves after
    the decode, `load` runs, the patched `AssertionError` propagates out of
    the ASGI app, and this test fails loudly instead of quietly passing.
    """
    side = math.isqrt(settings.IMAGE_DECODE_MAX_PIXELS) + 1
    payload = _png_header_only(side, side)
    assert side * side > settings.IMAGE_DECODE_MAX_PIXELS
    assert side * side < Image.MAX_IMAGE_PIXELS  # Pillow itself would not object
    assert len(payload) < 1024  # tiny on the wire, enormous once decoded

    def _load_should_not_be_called(self, *args, **kwargs):
        raise AssertionError("load called")

    monkeypatch.setattr(ImageFile.ImageFile, "load", _load_should_not_be_called)

    tid, uid, ids = agency_with_clients
    async with _client_for(tid, uid) as http:
        response = await _upload(http, ids["live"], payload)

    assert response.status_code == 400
    assert store.binary_objects == {}


async def test_a_canvas_under_the_decode_budget_still_succeeds(agency_with_clients, store):
    """The guard bounds the decode; it does not refuse ordinary logos."""
    assert 256 * 256 < settings.IMAGE_DECODE_MAX_PIXELS

    tid, uid, ids = agency_with_clients
    async with _client_for(tid, uid) as http:
        response = await _upload(http, ids["live"], _png_bytes(256, 256))

    assert response.status_code == 200


async def test_a_format_outside_the_allowlist_is_never_decoded(agency_with_clients, store):
    """`Image.open` is pinned to an explicit list of plugins.

    Unpinned, Pillow tries every registered plugin, EPS among them — and EPS
    shells out to ghostscript where it is installed. A TIFF stands in for any
    format off the list: real, decodable by Pillow, and refused here anyway.

    The message matters as much as the status code: a real TIFF is not
    corrupt, so the caller must be told which formats we do accept, not that
    their file is unreadable.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buffer, format="TIFF")

    tid, uid, ids = agency_with_clients
    async with _client_for(tid, uid) as http:
        response = await _upload(http, ids["live"], buffer.getvalue(), name="logo.tiff")

    assert response.status_code == 400
    assert store.binary_objects == {}
    detail = response.json()["detail"]
    for fmt in clients_logo._ALLOWED_FORMATS:
        assert fmt in detail
    assert "not a readable image" not in detail


async def test_corrupt_bytes_get_the_unreadable_message(agency_with_clients, store):
    """Truncated bytes are a different failure than an unlisted format.

    Bytes matching no format signature at all (plain garbage) also raise
    `UnidentifiedImageError`, so they are not a useful contrast here — Pillow
    cannot tell "wrong format" from "no format" once plugins are restricted.
    A PNG with a valid header but a body cut short parses as PNG, then fails
    inside `load()` with `OSError`: the real "malformed bytes" case. This
    proves the caller is told the bytes were corrupt, not that the format is
    merely unsupported — the two messages must not swap.
    """
    truncated = _png_bytes(64, 64)[:-30]  # valid header, body cut short
    tid, uid, ids = agency_with_clients
    async with _client_for(tid, uid) as http:
        response = await _upload(http, ids["live"], truncated, name="logo.png")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "not a readable image" in detail


async def test_exif_is_stripped(agency_with_clients, store):
    """Re-encoding IS the strip — nothing is copied from the original."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with _client_for(tid, uid) as http:
        await _upload(http, target, _jpeg_with_exif(), name="exif.jpg", type="image/jpeg")

    stored, _content_type = store.binary_objects[client_logo_key(tid, target)]
    exif = Image.open(io.BytesIO(stored)).getexif()
    assert dict(exif) == {}


async def test_another_agency_gets_404_on_every_verb(agency_with_clients, other_agency, store):
    tid, uid, _ids = agency_with_clients
    _other_tid, _other_uid, other_cid = other_agency
    async with _client_for(tid, uid) as http:
        assert (await _upload(http, other_cid, _png_bytes(10, 10))).status_code == 404
        assert (await http.get(f"/api/clients/{other_cid}/logo")).status_code == 404
        assert (await http.delete(f"/api/clients/{other_cid}/logo")).status_code == 404


async def test_failed_object_delete_leaves_the_key(agency_with_clients, store, monkeypatch):
    """So the delete can be retried. Nulling first would orphan the object
    with nothing left pointing at it."""
    tid, uid, ids = agency_with_clients
    target = ids["live"]

    async def _boom(_key: str) -> None:
        raise RuntimeError("R2 is unreachable")

    async with _client_for(tid, uid) as http:
        assert (await _upload(http, target, _png_bytes())).status_code == 200
        monkeypatch.setattr(store, "delete", _boom)
        with pytest.raises(RuntimeError):
            await http.delete(f"/api/clients/{target}/logo")
        detail = (await http.get(f"/api/clients/{target}")).json()
    assert detail["logo_key"] is not None


async def test_reupload_replaces_in_place_and_moves_the_timestamp(agency_with_clients, store):
    tid, uid, ids = agency_with_clients
    target = ids["live"]
    async with _client_for(tid, uid) as http:
        first = (await _upload(http, target, _png_bytes())).json()
        second = (await _upload(http, target, _png_bytes())).json()
    assert first["logo_key"] == second["logo_key"]  # deterministic key
    assert second["logo_updated_at"] > first["logo_updated_at"]


def test_the_logo_routes_live_under_api() -> None:
    """Anything outside /api is shadowed by the static site mounted at "/"."""
    paths = set(app.openapi()["paths"])
    assert "/api/clients/{client_id}/logo" in paths
    assert [p for p in paths if "logo" in p and not p.startswith("/api")] == []
