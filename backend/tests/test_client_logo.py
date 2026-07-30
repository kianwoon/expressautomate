"""Client logos: what may be stored, who may see it, and what shape it keeps.

The upload path is a near-mirror of `test_candidate_avatar.py`. The one
behavioural difference under test here: a logo is CONTAINED in a square, not
cropped to one — a wide wordmark must survive with transparent padding rather
than losing its edges.
"""

import io
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
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
