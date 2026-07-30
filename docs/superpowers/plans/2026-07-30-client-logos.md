# Client Logos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a client a company logo, uploaded by a recruiter, stored in R2, shown in the client panel and on the sourcing screen.

**Architecture:** Mirrors the candidate avatar end to end — one additive migration for two columns, a new `clients_logo.py` router beside `candidates_avatar.py`, a `client-logo.tsx` component beside `candidate-avatar.tsx`. The one behavioural difference is that a logo is **contained** in a square rather than cropped to one, because a wordmark centre-cropped is unreadable.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Pillow, boto3 against Cloudflare R2, pytest, Next.js static export, vitest.

**Spec:** [docs/superpowers/specs/2026-07-30-client-logos-design.md](../specs/2026-07-30-client-logos-design.md)

## Global Constraints

- **No hardcoded values.** Config comes from the repo-root `.env` via `app.core.config.settings`. Every new setting carries a default so a missing `.env` entry cannot stop `api` booting.
- **The object key is computed from the authenticated tenant, never received from the caller.** A caller-supplied key is a cross-tenant write.
- **The `{tenant_id}/` key prefix is load-bearing** — it is what a tenant purge sweeps by.
- **Another agency's id is a 404, never a 403.** "Exists but not yours" is itself a disclosure.
- **Every API route lives under `/api`;** `tests/test_routing.py` fails if one escapes, because the static frontend mount would shadow it.
- **No file exceeds 1500 lines.** `clients.py` is 863 and must not absorb this. `frontend/app/app.css` is already 1507 — new styles go in `frontend/app/dashboard/clients/clients.css`.
- **R2 is never contacted for real in tests.** Use the `InMemoryBodyStore` double through `app.dependency_overrides`, as `tests/test_candidate_avatar.py` does.
- **Run tests with `cd backend && scripts/test-env.sh -q`** — never hand-rolled env vars and never CI's values. CI uses a different app-role password; forcing it produces hundreds of bogus auth failures that read as flakiness. Lint with `uv run ruff check .`.
- **Baseline:** 1446 passed, 1 skipped. Alembic head `a0bfc93f7eb8`.
- **`agency_with_clients` yields a TUPLE** `(tenant_id, user_id, ids)`, not an object. Follow the `_client_for(tid, uid)` pattern the neighbouring tests use.
- **`tenant_session` sets `app.tenant_id` with `SET LOCAL`** — it is transaction-scoped. Never commit and then re-read on the same session.
- **Commit after every task.**

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/alembic/versions/<rev>_client_logo.py` (create) | `logo_key`, `logo_updated_at` on `clients`. |
| `backend/app/core/config.py` (modify) | Three `CLIENT_LOGO_*` settings. |
| `backend/app/services/storage/r2.py` (modify) | `client_logo_key(tenant_id, client_id)`. |
| `backend/app/models/client.py` (modify) | The two columns. |
| `backend/app/api/clients_logo.py` (create) | Upload, presign, delete. |
| `backend/app/main.py` (modify) | Register the router. |
| `backend/app/api/clients.py` (modify) | `_serialize` gains the two fields. |
| `backend/tests/test_client_logo.py` (create) | The whole surface, R2 faked. |
| `frontend/app/dashboard/clients.ts` (modify) | Three wrappers, a path helper, widened `Client`. |
| `frontend/app/dashboard/clients/client-logo.tsx` (create) | Render, upload, delete. |
| `frontend/app/dashboard/clients/client-panel.tsx` (modify) | The logo beside the name. |
| `frontend/app/dashboard/clients/clients.css` (modify) | Logo styles. |
| `frontend/app/dashboard/job-orders-sourcing.tsx` (modify) | Client name + logo in place of a bare id. |

---

## Task 1: Schema, settings, and the key helper

**Files:**
- Create: `backend/alembic/versions/<rev>_client_logo.py`
- Modify: `backend/app/models/client.py`, `backend/app/core/config.py:182-190` (beside the AVATAR settings), `backend/app/services/storage/r2.py:71-80` (beside `avatar_key`), `backend/app/api/clients.py` (`_serialize`)
- Test: `backend/tests/test_client_logo.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Client.logo_key: str | None`, `Client.logo_updated_at: datetime | None`; `client_logo_key(tenant_id: uuid.UUID, client_id: uuid.UUID) -> str`; settings `CLIENT_LOGO_MAX_UPLOAD_BYTES`, `CLIENT_LOGO_MAX_PIXEL_DIMENSION`, `CLIENT_LOGO_PRESIGNED_URL_TTL_SECONDS`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_client_logo.py`:

```python
def test_client_logo_key_is_tenant_prefixed():
    """The `{tenant_id}/` prefix is what a tenant purge sweeps by. A key
    without it survives an erasure request."""
    tenant = uuid.UUID("11111111-1111-1111-1111-111111111111")
    client = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert client_logo_key(tenant, client) == f"{tenant}/clients/{client}/logo"
```

Import it from `app.services.storage.r2`.

- [ ] **Step 2: Run it and watch it fail**

```bash
cd backend && scripts/test-env.sh -q tests/test_client_logo.py -v
```

Expected: FAIL — `ImportError: cannot import name 'client_logo_key'`.

- [ ] **Step 3: Add the key helper**

In `backend/app/services/storage/r2.py`, immediately after `avatar_key`:

```python
def client_logo_key(tenant_id: uuid.UUID, client_id: uuid.UUID) -> str:
    """Deterministic object key for a client's logo.

    Same shape and the same reason as `avatar_key`: the leading `{tenant_id}/`
    segment is what a tenant erasure purges by, so an object stored without it
    would survive the request that was supposed to remove it.

    Deterministic, so re-uploading replaces the bytes in place rather than
    accumulating one object per upload with nothing pointing at the old ones.
    """
    return f"{tenant_id}/clients/{client_id}/logo"
```

- [ ] **Step 4: Add the settings**

In `backend/app/core/config.py`, beside the `AVATAR_*` block at line 182:

```python
    # A company logo, not a passport photo: its own limits, because the two
    # have no reason to move together and a shared constant would only reveal
    # the coupling when changing one broke the other.
    CLIENT_LOGO_MAX_UPLOAD_BYTES: int = Field(default=5 * 1024 * 1024, gt=0)
    CLIENT_LOGO_MAX_PIXEL_DIMENSION: int = Field(default=1024, gt=0)
    CLIENT_LOGO_PRESIGNED_URL_TTL_SECONDS: int = Field(default=300, gt=0)
```

There is deliberately **no** `CLIENT_LOGO_STORED_FORMAT`. The logo is always PNG — see Task 2 Step 5.

- [ ] **Step 5: Add the columns to the model**

In `backend/app/models/client.py`, inside `class Client` after `source`:

```python
    # The logo lives in R2; this names it. Nullable because most clients are
    # proposed by the pipeline and will never have one.
    logo_key: Mapped[str | None] = mapped_column(Text)
    # Lets the browser bust its own image cache without re-reading the object,
    # the same reason `candidates.avatar_updated_at` exists.
    logo_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 6: Serialize them**

In `backend/app/api/clients.py`, add to `_serialize`'s returned dict:

```python
        "logo_key": client.logo_key,
        "logo_updated_at": (
            client.logo_updated_at.isoformat() if client.logo_updated_at else None
        ),
```

- [ ] **Step 7: Generate and check the migration**

```bash
cd backend && uv run alembic revision --autogenerate -m "client logo"
```

Expected: two `op.add_column` calls on `clients`, both nullable, and a `down_revision` of `a0bfc93f7eb8`. Delete anything else autogenerate proposes — drift against this schema is expected and is not yours to ship. Confirm `downgrade()` drops both columns.

- [ ] **Step 8: Apply and run**

```bash
cd backend && uv run alembic upgrade head && scripts/test-env.sh -q && uv run ruff check .
```

Expected: PASS. Baseline is 1446 passed, 1 skipped; you have added one test.

- [ ] **Step 9: Commit**

```bash
git add backend/alembic/versions backend/app/models/client.py backend/app/core/config.py backend/app/services/storage/r2.py backend/app/api/clients.py backend/tests/test_client_logo.py
git commit -m "feat: schema and object key for client logos"
```

---

## Task 2: The upload, presign and delete endpoints

**Files:**
- Create: `backend/app/api/clients_logo.py`
- Modify: `backend/app/main.py` (register the router)
- Test: `backend/tests/test_client_logo.py`

**Interfaces:**
- Consumes: `client_logo_key`, the three settings, the two columns (Task 1); `_load` from `app.api.clients`; `BodyStore` / `R2BodyStore` from `app.services.storage.r2`.
- Produces: `POST /clients/{client_id}/logo` → `{logo_key, logo_updated_at}`; `GET /clients/{client_id}/logo` → `{url, expires_in}`; `DELETE /clients/{client_id}/logo` → 204. Also `body_store()` as a FastAPI dependency, so tests can override it.

**Read first:** `backend/app/api/candidates_avatar.py` in full, and `backend/tests/test_candidate_avatar.py`. This task is that file with one behavioural change; the closer the two read, the better.

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_client_logo.py`, following `test_candidate_avatar.py` for the `InMemoryBodyStore` double and the `app.dependency_overrides` setup (copy its fixture rather than inventing one):

```python
@pytest.mark.asyncio
async def test_upload_then_get_then_delete(client, agency_with_clients, fake_store):
    target = _client_for(...)  # follow the neighbouring tests' fixture shape

    uploaded = await client.post(
        f"/api/clients/{target}/logo",
        files={"file": ("logo.png", _png_bytes(300, 100), "image/png")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["logo_key"].startswith(str(tenant_id))

    presigned = await client.get(f"/api/clients/{target}/logo")
    assert presigned.status_code == 200
    assert presigned.json()["expires_in"] == settings.CLIENT_LOGO_PRESIGNED_URL_TTL_SECONDS

    assert (await client.delete(f"/api/clients/{target}/logo")).status_code == 204
    assert (await client.get(f"/api/clients/{target}/logo")).status_code == 404


@pytest.mark.asyncio
async def test_a_wide_logo_is_contained_not_cropped(client, agency_with_clients, fake_store):
    """The rule that distinguishes a logo from a candidate photo.

    A 400x100 wordmark centre-cropped to a square loses two thirds of the
    words. Contained, it survives with transparent padding above and below.
    """
    target = _client_for(...)
    await client.post(
        f"/api/clients/{target}/logo",
        files={"file": ("wide.png", _png_bytes(400, 100), "image/png")},
    )

    stored = Image.open(io.BytesIO(fake_store.last_body()))
    assert stored.width == stored.height          # square canvas
    assert stored.mode == "RGBA"                  # alpha, so the padding is transparent
    assert stored.format == "PNG"

    # The original content is intact and centred, not cropped: the padding rows
    # at the very top are fully transparent and the middle band is not.
    assert stored.getpixel((stored.width // 2, 2))[3] == 0
    assert stored.getpixel((stored.width // 2, stored.height // 2))[3] != 0


@pytest.mark.asyncio
async def test_oversized_upload_is_413_before_decode(client, agency_with_clients, fake_store):
    target = _client_for(...)
    too_big = b"x" * (settings.CLIENT_LOGO_MAX_UPLOAD_BYTES + 1024)
    response = await client.post(
        f"/api/clients/{target}/logo", files={"file": ("big.png", too_big, "image/png")}
    )
    # 413, not 400: it was never decoded, so "not a readable image" would be
    # the wrong answer even though these bytes are not one.
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_a_non_image_is_400_not_500(client, agency_with_clients, fake_store):
    target = _client_for(...)
    response = await client.post(
        f"/api/clients/{target}/logo", files={"file": ("cv.pdf", b"%PDF-1.4 not an image", "image/png")}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_exif_is_stripped(client, agency_with_clients, fake_store):
    """Re-encoding IS the strip — nothing is copied from the original."""
    target = _client_for(...)
    await client.post(
        f"/api/clients/{target}/logo",
        files={"file": ("exif.jpg", _jpeg_with_exif(), "image/jpeg")},
    )
    stored = Image.open(io.BytesIO(fake_store.last_body()))
    assert not stored.getexif()


@pytest.mark.asyncio
async def test_another_agency_gets_404_on_every_verb(client, other_agency_client_id, fake_store):
    assert (await client.post(
        f"/api/clients/{other_agency_client_id}/logo",
        files={"file": ("l.png", _png_bytes(10, 10), "image/png")},
    )).status_code == 404
    assert (await client.get(f"/api/clients/{other_agency_client_id}/logo")).status_code == 404
    assert (await client.delete(f"/api/clients/{other_agency_client_id}/logo")).status_code == 404


@pytest.mark.asyncio
async def test_failed_object_delete_leaves_the_key(client, agency_with_clients, failing_store):
    """So the delete can be retried. Nulling first would orphan the object with
    nothing left pointing at it."""
    target = _client_for(...)
    ...  # upload with the working store, then swap in a store whose delete raises
    with pytest.raises(Exception):
        await client.delete(f"/api/clients/{target}/logo")
    detail = (await client.get(f"/api/clients/{target}")).json()
    assert detail["logo_key"] is not None


@pytest.mark.asyncio
async def test_reupload_replaces_in_place_and_moves_the_timestamp(client, agency_with_clients, fake_store):
    target = _client_for(...)
    first = (await client.post(f"/api/clients/{target}/logo", files={...})).json()
    second = (await client.post(f"/api/clients/{target}/logo", files={...})).json()
    assert first["logo_key"] == second["logo_key"]          # deterministic key
    assert second["logo_updated_at"] > first["logo_updated_at"]
```

Write the `_png_bytes(w, h)` and `_jpeg_with_exif()` helpers with Pillow at the top of the module. Fill the elided fixture access from the neighbouring tests — do not invent a shape.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && scripts/test-env.sh -q tests/test_client_logo.py -v
```

Expected: FAIL — 404/405 on `/api/clients/{id}/logo`; the route does not exist.

- [ ] **Step 3: Create the router module**

`backend/app/api/clients_logo.py`, opening with a docstring that states the three rules (key computed not received; client resolved through the tenant session; nothing the client says about the file is believed) and names the one difference from `candidates_avatar.py`: contained, not cropped.

Copy `body_store()`, `_read_within_limit()` and the endpoint structure from `candidates_avatar.py`, substituting the `CLIENT_LOGO_*` settings, `client_logo_key`, `Client`, and `_load` from `app.api.clients`.

- [ ] **Step 4: Write the containing re-encode**

This is the only genuinely new code in the task:

```python
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
            # PNG is not a setting here, unlike the candidate path. JPEG has no
            # alpha channel, so a format setting that ever moved off PNG would
            # silently turn every logo's padding black.
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
```

The stored MIME type is `"image/png"` — a literal here rather than a Pillow registry lookup, because the format is a literal too.

- [ ] **Step 5: Register the router**

In `backend/app/main.py`, beside where `candidates_avatar`'s router is included, following whatever prefix and tag pattern that line uses.

- [ ] **Step 6: Run the tests**

```bash
cd backend && scripts/test-env.sh -q tests/test_client_logo.py -v
cd backend && scripts/test-env.sh -q tests/test_routing.py -v
cd backend && scripts/test-env.sh -q && uv run ruff check .
```

Expected: all PASS. `test_routing` matters — a route that escaped `/api` would be shadowed by the static mount.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/clients_logo.py backend/app/main.py backend/tests/test_client_logo.py
git commit -m "feat: upload, presign and remove a client logo"
```

---

## Task 3: The logo in the client panel

**Files:**
- Create: `frontend/app/dashboard/clients/client-logo.tsx`
- Modify: `frontend/app/dashboard/clients.ts`, `frontend/app/dashboard/clients/client-panel.tsx`, `frontend/app/dashboard/clients/clients.css`
- Test: `frontend/app/dashboard/clients/client-logo.test.tsx`

**Interfaces:**
- Consumes: the three endpoints from Task 2.
- Produces: `uploadClientLogo(id, file): Promise<{logo_key, logo_updated_at}>`, `getClientLogo(id): Promise<{url, expires_in}>`, `deleteClientLogo(id): Promise<void>`, `clientLogoPath(id): string`; `<ClientLogo client={client} onChange={() => void} />`.

**Read first:** `frontend/app/dashboard/candidates/candidate-avatar.tsx` and the avatar wrappers in `candidates.ts`. Follow both.

- [ ] **Step 1: Add the wrappers and widen the type**

In `frontend/app/dashboard/clients.ts`, using the same fetch helper and `readError`/`readProblem` path as the existing wrappers — do not introduce a second idiom. `Client` gains `logo_key: string | null` and `logo_updated_at: string | null`, matching `_serialize`.

- [ ] **Step 2: Build the component**

`client-logo.tsx`, mirroring `candidate-avatar.tsx`:

- The mark itself is the control: a camera overlay on hover **and focus**, over a real `<input type="file">`. No separate Upload button, and it must be keyboard-operable.
- Contained in a **rounded square**, not a circle.
- Empty state is the client's **initials** ("MP" for Meridian Partners), never a generic building glyph — a placeholder icon reads as a failed load.
- The presigned URL is fetched fresh when the component mounts or the client id changes, and **never persisted**. It expires in 300s: a cached one is a broken image, a stored one is a leaked capability.
- A delete control, only when a logo exists.

Styles go in `clients.css`. `frontend/app/app.css` is at 1507 lines, already past the repo's own ceiling.

- [ ] **Step 3: Place it in the panel**

In `client-panel.tsx`, at the top beside the client name. On upload or delete, refresh the client so `logo_updated_at` moves.

- [ ] **Step 4: Write the tests**

`client-logo.test.tsx`, following `client-form.test.tsx` for setup:
- With `logo_key: null`, the initials render and no logo request fires.
- With a logo, the presigned URL is requested once and the returned URL reaches the `<img>`.
- Upload posts to `/api/clients/{id}/logo` as multipart and calls `onChange`.
- Delete calls the DELETE endpoint and returns to the initials state.

- [ ] **Step 5: Run**

```bash
cd frontend && npx tsc --noEmit && npx vitest run && npm run build
```

Expected: all three pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/dashboard/clients.ts frontend/app/dashboard/clients
git commit -m "feat: show and manage a client logo in the panel"
```

---

## Task 4: The client on the sourcing screen

**Files:**
- Modify: `frontend/app/dashboard/job-orders-sourcing.tsx:250-260, 344-352`
- Test: whichever vitest module covers that component; create one if none exists.

**Interfaces:**
- Consumes: `<ClientLogo>` (Task 3), `getClient` (existing).

**The problem being fixed:** that screen currently shows `run.client_id` — a bare UUID — as the only identification of which client the already-submitted exclusion was applied for. A logo beside a UUID is a puzzle, so this adds the **name** as well.

- [ ] **Step 1: Write the failing test**

- A run with a `client_id` renders the client's name and its logo.
- A run with `client_id: null` renders **neither**, fires no client request, and still renders the existing unresolved notice with `client_unresolved_reason` unchanged.

- [ ] **Step 2: Run it and watch it fail**

```bash
cd frontend && npx vitest run
```

Expected: FAIL — the name is not rendered today.

- [ ] **Step 3: Implement**

Fetch the client by `run.client_id` when it is non-null, render its name with `<ClientLogo>` beside it. Guard every path on `client_id` being null — that case is reachable and already has its own notice, which must not change.

- [ ] **Step 4: Run**

```bash
cd frontend && npx tsc --noEmit && npx vitest run && npm run build
```

- [ ] **Step 5: Commit**

```bash
git add frontend/app/dashboard/job-orders-sourcing.tsx frontend/app/dashboard
git commit -m "feat: name the client on the sourcing screen, with its logo"
```

---

## Final verification

- [ ] **Backend**

```bash
cd backend && scripts/test-env.sh -q && uv run ruff check .
```

Expected: green. Quote the summary line. Baseline was 1446 passed, 1 skipped.

- [ ] **Migration round-trips**

```bash
cd backend && uv run alembic downgrade -1 && uv run alembic upgrade head
```

Expected: both succeed.

- [ ] **Routing**

```bash
cd backend && scripts/test-env.sh -q tests/test_routing.py -v
```

- [ ] **Frontend**

```bash
cd frontend && npx tsc --noEmit && npx vitest run && npm run build
```

- [ ] **Env check before deploy**

`R2_ENDPOINT_URL`, `R2_SECRET_ACCESS_KEY` and `R2_BUCKET_NAME` must be present on the `api` service. They should be — candidate avatar upload already needs them, and their absence was a recorded outage — but `CLAUDE.md` says to check the service's env before shipping anything touching an external system, and this is that check:

```bash
koyeb deployment get $(koyeb deployments list --service <api-id> -o json | jq -r '.deployments[0].id') -o json | jq -r '.deployment.definition.env[].key' | grep R2_
```
