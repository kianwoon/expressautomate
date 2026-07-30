"""Candidate photos: what may be stored, who may see it, and what is stripped.

Every test here is about a boundary rather than a feature. The upload path
takes bytes from a browser and writes them to shared object storage under a
tenant-prefixed key, so the interesting questions are all adversarial: can a
caller name someone else's key, can they get a URL they should not have, and
does a phone photo's GPS survive the round trip.
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

from app.api import candidates_avatar
from app.core.config import settings
from app.main import app
from app.services.storage.r2 import InMemoryBodyStore, avatar_key
from tests.conftest import AdminSessionLocal
from tests.test_clients_api import sign_in  # the real session cookie, not a copy


async def _seed_agency() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid, uid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with AdminSessionLocal() as s:
        await s.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:i, :n, :n)"),
            {"i": tid, "n": f"agency-{tid.hex[:6]}"},
        )
        await s.execute(
            text("INSERT INTO users (id, tenant_id, email, role) VALUES (:i, :t, :e, 'owner')"),
            {"i": uid, "t": tid, "e": f"u{uid.hex[:6]}@agency.sg"},
        )
        await s.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, pipeline_stage, "
                "record_status) VALUES (:i, :t, 'Jane Tan', 'new', 'active')"
            ),
            {"i": cid, "t": tid},
        )
        await s.commit()
    return tid, uid, cid


async def _drop_agency(tid: uuid.UUID) -> None:
    async with AdminSessionLocal() as s:
        for table in ("candidate_field_overrides", "candidate_skills", "candidates", "users"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tid})
        await s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})
        await s.commit()


@pytest.fixture
async def store():
    """The object store, swapped for the in-memory double.

    Overridden through `app.dependency_overrides` rather than by patching a
    module global: the override is undone here even when a test fails, so a
    failure can never leave the next test writing to real R2.
    """
    double = InMemoryBodyStore()
    app.dependency_overrides[candidates_avatar.body_store] = lambda: double
    yield double
    app.dependency_overrides.pop(candidates_avatar.body_store, None)


@pytest.fixture
async def agency():
    tid, uid, cid = await _seed_agency()
    yield tid, uid, cid
    await _drop_agency(tid)


@pytest.fixture
async def other_agency():
    """A second agency, so "not yours" can be told apart from "not there"."""
    tid, uid, cid = await _seed_agency()
    yield tid, uid, cid
    await _drop_agency(tid)


def _client_for(tid: uuid.UUID, uid: uuid.UUID) -> AsyncClient:
    http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    sign_in(http, uid, tid)
    return http


def _png_bytes(size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_with_gps() -> bytes:
    """A JPEG carrying the EXIF a phone camera writes, GPS included.

    Built rather than committed as a fixture file so the tag values are visible
    right here — the test asserts on these exact strings surviving or not.
    """
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMake"  # Make
    exif[0x0110] = "SecretCameraModel"  # Model
    # GPS IFD: latitude ref + a whole-degree latitude. The point is only that
    # a location is present in the original bytes.
    exif[0x8825] = {1: "N", 2: (1.0, 23.0, 0.0)}
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), (200, 30, 30)).save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


async def _upload(http: AsyncClient, cid: uuid.UUID, content: bytes, **kwargs):
    return await http.post(
        f"/api/candidates/{cid}/avatar",
        files={"file": (kwargs.get("name", "photo.png"), content, kwargs.get("type", "image/png"))},
    )


# --- tenant isolation -------------------------------------------------------


async def test_another_agencys_candidate_is_missing_not_forbidden(agency, other_agency, store):
    """404 on all three verbs. A 403 would confirm the id exists."""
    tid, uid, _ = agency
    _other_tid, _other_uid, other_cid = other_agency
    async with _client_for(tid, uid) as http:
        assert (await _upload(http, other_cid, _png_bytes())).status_code == 404
        assert (await http.get(f"/api/candidates/{other_cid}/avatar")).status_code == 404
        assert (await http.delete(f"/api/candidates/{other_cid}/avatar")).status_code == 404


async def test_a_rejected_cross_tenant_upload_writes_nothing(agency, other_agency, store):
    """The 404 must come before the write, not after it."""
    tid, uid, _ = agency
    other_tid, _other_uid, other_cid = other_agency
    async with _client_for(tid, uid) as http:
        await _upload(http, other_cid, _png_bytes())
    assert store.binary_objects == {}
    assert avatar_key(other_tid, other_cid) not in store.binary_objects


async def test_one_agency_cannot_read_anothers_stored_photo(agency, other_agency, store):
    """Even with the object present under the other tenant's key."""
    other_tid, other_uid, other_cid = other_agency
    async with _client_for(other_tid, other_uid) as http:
        assert (await _upload(http, other_cid, _png_bytes())).status_code == 200

    tid, uid, _ = agency
    async with _client_for(tid, uid) as http:
        assert (await http.get(f"/api/candidates/{other_cid}/avatar")).status_code == 404
    # And the object survived the attempt.
    assert avatar_key(other_tid, other_cid) in store.binary_objects


# --- what may be uploaded ---------------------------------------------------


async def test_an_oversized_photo_is_refused(agency, store, monkeypatch):
    monkeypatch.setattr(settings, "AVATAR_MAX_UPLOAD_BYTES", 512)
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, _png_bytes((600, 600)))
    assert response.status_code == 413
    assert store.binary_objects == {}


async def test_a_file_that_is_not_an_image_is_a_4xx_not_a_500(agency, store):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, b"%PDF-1.7\nnot an image at all\n")
    assert response.status_code == 400
    assert store.binary_objects == {}


async def test_the_declared_content_type_is_not_believed(agency, store):
    """`image/png` on a text body is still a 400 — the bytes decide."""
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, b"totally not an image", type="image/png")
    assert response.status_code == 400


async def test_an_extension_that_lies_about_the_bytes_is_refused(agency, store):
    """`.png` on a zip archive. Nothing here looks at the filename."""
    tid, uid, cid = agency
    payload = b"PK\x03\x04" + b"\x00" * 64
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, payload, name="portrait.png", type="image/png")
    assert response.status_code == 400
    assert store.binary_objects == {}


async def test_a_real_jpeg_named_png_is_accepted_on_its_bytes(agency, store):
    """The mirror image: the extension lies, but the bytes are a real photo."""
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, _jpeg_with_gps(), name="portrait.png")
    assert response.status_code == 200


async def test_an_implausible_canvas_is_refused_before_it_is_allocated(agency, store):
    """A decompression bomb: a tiny file declaring an enormous image.

    Pillow raises DecompressionBombError from `open`, which reads the header
    only — so the refusal costs no pixel buffer. It must surface as a 400.
    """
    monkey = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = 16  # any real photo now counts as a bomb
        tid, uid, cid = agency
        async with _client_for(tid, uid) as http:
            response = await _upload(http, cid, _png_bytes((256, 256)))
        assert response.status_code == 400
        assert store.binary_objects == {}
    finally:
        Image.MAX_IMAGE_PIXELS = monkey


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
    agency, store, monkeypatch
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

    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, payload)

    assert response.status_code == 400
    assert store.binary_objects == {}


async def test_a_canvas_under_the_decode_budget_still_succeeds(agency, store):
    """The guard bounds the decode; it does not refuse ordinary photos."""
    content = _png_bytes((256, 256))
    assert 256 * 256 < settings.IMAGE_DECODE_MAX_PIXELS

    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, content)

    assert response.status_code == 200


async def test_a_format_outside_the_allowlist_is_never_decoded(agency, store):
    """`Image.open` is pinned to an explicit list of plugins.

    Unpinned, Pillow tries every registered plugin, EPS among them — and EPS
    shells out to ghostscript where it is installed. A TIFF stands in for any
    format off the list: real, decodable by Pillow, and refused here anyway.

    The message matters as much as the status code: a real TIFF (an iPhone's
    HEIC lands the same way) is not corrupt, so the caller must be told which
    formats we do accept, not that their file is unreadable.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buffer, format="TIFF")

    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, buffer.getvalue(), name="p.tiff")

    assert response.status_code == 400
    assert store.binary_objects == {}
    detail = response.json()["detail"]
    for fmt in candidates_avatar._ALLOWED_FORMATS:
        assert fmt in detail
    assert "not a readable image" not in detail


async def test_corrupt_bytes_get_the_unreadable_message(agency, store):
    """Truncated bytes are a different failure than an unlisted format.

    Bytes matching no format signature at all (plain garbage) also raise
    `UnidentifiedImageError`, so they are not a useful contrast here — Pillow
    cannot tell "wrong format" from "no format" once plugins are restricted.
    A PNG with a valid header but a body cut short parses as PNG, then fails
    inside `load()` with `OSError`: the real "malformed bytes" case. This
    proves the caller is told the bytes were corrupt, not that the format is
    merely unsupported — the two messages must not swap.
    """
    truncated = _png_bytes((64, 64))[:-30]  # valid header, body cut short
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        response = await _upload(http, cid, truncated, name="p.png")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "not a readable image" in detail


# --- what is stored ---------------------------------------------------------


async def test_exif_does_not_survive_the_upload(agency, store):
    """The whole reason the image is re-encoded rather than passed through."""
    tid, uid, cid = agency
    original = _jpeg_with_gps()
    assert b"SecretCameraMake" in original  # the fixture is doing its job
    assert Image.open(io.BytesIO(original)).getexif().get(0x010F) == "SecretCameraMake"

    async with _client_for(tid, uid) as http:
        posted = await _upload(http, cid, original, name="p.jpg", type="image/jpeg")
    assert posted.status_code == 200

    stored, _content_type = store.binary_objects[avatar_key(tid, cid)]
    assert stored != original
    assert b"SecretCameraMake" not in stored
    assert b"SecretCameraModel" not in stored
    exif = Image.open(io.BytesIO(stored)).getexif()
    assert dict(exif) == {}
    assert 0x8825 not in exif  # no GPS IFD


async def test_the_key_is_tenant_prefixed_and_server_derived(agency, store):
    """Nothing the client sent contributes to the key."""
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        body = (await _upload(http, cid, _png_bytes())).json()

    expected = f"{tid}/candidates/{cid}/avatar"
    assert body["avatar_key"] == expected
    assert list(store.binary_objects) == [expected]
    assert expected.startswith(f"{tid}/")  # what tenant erasure purges by


async def test_large_photos_are_scaled_down_to_the_configured_bound(agency, store, monkeypatch):
    monkeypatch.setattr(settings, "AVATAR_MAX_PIXEL_DIMENSION", 32)
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        assert (await _upload(http, cid, _png_bytes((400, 200)))).status_code == 200
    stored, _ = store.binary_objects[avatar_key(tid, cid)]
    assert max(Image.open(io.BytesIO(stored)).size) == 32


async def test_the_stored_content_type_matches_the_stored_format(agency, store):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        await _upload(http, cid, _jpeg_with_gps(), name="p.jpg", type="image/jpeg")
    stored, content_type = store.binary_objects[avatar_key(tid, cid)]
    fmt = settings.AVATAR_STORED_FORMAT.upper()
    assert Image.open(io.BytesIO(stored)).format == fmt
    assert content_type == Image.MIME[fmt]


async def test_the_row_records_the_key_and_the_time(agency, store):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        body = (await _upload(http, cid, _png_bytes())).json()
    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text("SELECT avatar_key, avatar_updated_at FROM candidates WHERE id = :i"),
                {"i": cid},
            )
        ).one()
    assert row[0] == body["avatar_key"]
    assert row[1] is not None


# --- reading ----------------------------------------------------------------


async def test_a_candidate_with_no_photo_is_a_404(agency, store):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        assert (await http.get(f"/api/candidates/{cid}/avatar")).status_code == 404


async def test_the_url_ttl_comes_from_settings(agency, store, monkeypatch):
    monkeypatch.setattr(settings, "AVATAR_PRESIGNED_URL_TTL_SECONDS", 42)
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        await _upload(http, cid, _png_bytes())
        body = (await http.get(f"/api/candidates/{cid}/avatar")).json()
    assert body["expires_in"] == 42
    # The double encodes the TTL it was handed, so this proves the setting
    # reached the signer rather than only the response body.
    assert "ttl=42" in body["url"]


async def test_the_url_is_signed_per_request_and_never_stored(agency, store):
    """Nothing persists a URL: the row holds a key, and only a key."""
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        await _upload(http, cid, _png_bytes())
        first = (await http.get(f"/api/candidates/{cid}/avatar")).json()["url"]
    async with AdminSessionLocal() as s:
        stored_key = (
            await s.execute(
                text("SELECT avatar_key FROM candidates WHERE id = :i"), {"i": cid}
            )
        ).scalar_one()
    assert stored_key == avatar_key(tid, cid)
    assert "http" not in stored_key
    assert first.startswith("http")


# --- removal ----------------------------------------------------------------


async def test_delete_clears_both_the_object_and_the_columns(agency, store):
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        await _upload(http, cid, _png_bytes())
        assert avatar_key(tid, cid) in store.binary_objects
        assert (await http.delete(f"/api/candidates/{cid}/avatar")).status_code == 204

    assert store.binary_objects == {}
    async with AdminSessionLocal() as s:
        row = (
            await s.execute(
                text("SELECT avatar_key, avatar_updated_at FROM candidates WHERE id = :i"),
                {"i": cid},
            )
        ).one()
    assert row == (None, None)


async def test_deleting_a_photo_that_is_not_there_is_still_a_204(agency, store):
    """A double-click must not read as an error."""
    tid, uid, cid = agency
    async with _client_for(tid, uid) as http:
        assert (await http.delete(f"/api/candidates/{cid}/avatar")).status_code == 204


# --- routing ----------------------------------------------------------------


def test_the_avatar_routes_live_under_api() -> None:
    """Anything outside /api is shadowed by the static site mounted at "/"."""
    paths = set(app.openapi()["paths"])
    assert "/api/candidates/{candidate_id}/avatar" in paths
    assert [p for p in paths if "avatar" in p and not p.startswith("/api")] == []


async def test_the_routes_require_a_session(agency, store):
    """No cookie is a 401 — never an anonymous read of somebody's photo."""
    _tid, _uid, cid = agency
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get(f"/api/candidates/{cid}/avatar")).status_code == 401
        assert (await _upload(http, cid, _png_bytes())).status_code == 401
        assert (await http.delete(f"/api/candidates/{cid}/avatar")).status_code == 401


# --- storage the operator has to fix ---------------------------------------


async def test_a_misconfigured_store_is_a_503_not_a_500(agency, monkeypatch):
    """The production incident: the `api` service had no `R2_*` variables, so
    the client could not even be built and the upload failed with a bare 500.

    503 says the request was fine and the service is not, which is both true
    and the difference between an operator checking env vars and a developer
    hunting a code bug.
    """
    from app.services.storage.r2 import R2BodyStore

    monkeypatch.setattr(settings, "R2_ENDPOINT_URL", "")
    app.dependency_overrides[candidates_avatar.body_store] = R2BodyStore
    try:
        tid, uid, cid = agency
        async with _client_for(tid, uid) as http:
            response = await _upload(http, cid, _png_bytes())
    finally:
        app.dependency_overrides.pop(candidates_avatar.body_store, None)

    assert response.status_code == 503
    assert response.json()["detail"] == "Photo storage is unavailable."


async def test_the_503_never_names_the_bucket_or_the_endpoint(agency, monkeypatch):
    """The exception message carries the bucket and endpoint on purpose — an
    operator needs them. A client must not be told either: infrastructure
    names are reconnaissance, and the same handler covers unauthenticated
    endpoints too."""
    from app.services.storage.r2 import R2BodyStore

    monkeypatch.setattr(settings, "R2_ENDPOINT_URL", "")
    app.dependency_overrides[candidates_avatar.body_store] = R2BodyStore
    try:
        tid, uid, cid = agency
        async with _client_for(tid, uid) as http:
            response = await _upload(http, cid, _png_bytes())
    finally:
        app.dependency_overrides.pop(candidates_avatar.body_store, None)

    body = response.text
    for secret in (
        settings.R2_BUCKET_NAME,
        settings.R2_ACCESS_KEY_ID,
        settings.R2_SECRET_ACCESS_KEY,
    ):
        if secret:
            assert secret not in body
    assert "R2_" not in body
    assert "endpoint" not in body.lower()
