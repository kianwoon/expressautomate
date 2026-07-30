# Client logos

Decided 2026-07-30. Follows
[the clients administration design](2026-07-30-clients-administration-design.md),
which gave a recruiter the client list as something they own rather than
merely review.

A candidate has an avatar. A client does not, so the client list is a wall of
text where the candidate list is scannable. This adds the company logo, reusing
the candidate avatar machinery rather than inventing a second one.

## Decisions

| Question | Decision |
|---|---|
| Shape | **Contained** in a rounded square, never cropped. A wordmark must stay readable. |
| Where the image comes from | **Manual upload only.** No logo-fetching vendor. |
| Where it appears | The client detail panel, and the sourcing screen where a run names its client. **Not** the clients table. |
| Storage | R2, the same bucket and credentials the candidate avatar already uses. |

### Why not auto-fetch from the email domain

A client already carries `email_domain`, so a vendor like Clearbit or
Brandfetch could fill this in with no recruiter effort. Rejected on three
counts, any one of which is sufficient: it is a **new external system**, and
this repo has two recorded outages from a service making its first call to one
(`GRAPH_BASE_URL` on `api`, then `R2_*` — see the deploy section of
`CLAUDE.md`); it sends the agency's client list to a third party, which is not
ours to give; and it puts a guessed image on a record, which is the same class
of mistake as §15's "do not fabricate missing values".

### Why not the clients table

Every logo on screen costs one presigned-URL request, because a presigned URL
is generated per request and never cached. A table of 200 rows is 200 requests
from one screen. Either the list endpoint would have to return presigned URLs
in bulk — putting 200 signed capabilities in one payload, most of them never
used — or the table would fire a request per row. The panel and the sourcing
screen each show exactly one client, so neither problem arises there. If the
table is wanted later it needs its own design, not an extension of this one.

## Backend

New file `backend/app/api/clients_logo.py`. It does **not** go in
`clients.py`, which is 863 lines after the administration work, and it mirrors
`backend/app/api/candidates_avatar.py` closely enough that the two should be
readable side by side.

| Endpoint | Returns | Status codes |
|---|---|---|
| `POST /clients/{client_id}/logo` | `{logo_key, logo_updated_at}` | 200, 400 unreadable image, 413 too large, 404 |
| `GET /clients/{client_id}/logo` | `{url, expires_in}` | 200, 404 no logo |
| `DELETE /clients/{client_id}/logo` | — | 204 |

### Reused from the candidate avatar, deliberately unchanged

- **The size check happens before the decode**
  ([candidates_avatar.py:94-127](../../../backend/app/api/candidates_avatar.py)).
  Decoding first would let a small file with a huge declared canvas allocate
  before anything checked it.
- **Pillow re-encodes**, which strips EXIF and ICC data. An uploaded image is
  never stored as received.
- **The decompression-bomb guard**: `Image.open()` reads the header first.
- **The tenant check runs before the bytes are touched**, so another agency's
  id is a 404 that never even reads the upload.
- **Delete the object, then null the columns**
  ([candidates_avatar.py:221-228](../../../backend/app/api/candidates_avatar.py)).
  The reverse order orphans the object in R2 with nothing left pointing at it;
  this order leaves, on failure, a row that still names the key so the delete
  can be retried.

### Three deliberate differences

**1. The key is `{tenant_id}/clients/{client_id}/logo`.** The `{tenant_id}/`
prefix is load-bearing — it is what makes a tenant purge sweepable by prefix —
and the key is **computed from the session, never accepted from the caller**.
A caller-supplied key is a cross-tenant read.

**2. Contained, not cropped.** The candidate path crops to a square, which is
right for a face and wrong for a wordmark: "Meridian Partners Pte Ltd" centre-
cropped to a square is unreadable. The logo is scaled to fit inside
`CLIENT_LOGO_MAX_DIMENSION` on its longest side and padded with transparency to
a square. Nothing is upscaled — a 64px logo stays 64px, letterboxed.

This makes **PNG a requirement rather than a default**. JPEG has no alpha
channel, so the padding would come out black. The candidate path's
`AVATAR_IMAGE_FORMAT` setting happens to default to PNG; this path does not
read that setting at all, because a future change to it must not silently turn
every client logo's padding black.

**3. Its own settings, each with a default:**
`CLIENT_LOGO_MAX_UPLOAD_BYTES`, `CLIENT_LOGO_MAX_DIMENSION`,
`CLIENT_LOGO_PRESIGNED_URL_TTL_SECONDS`. Sharing the candidate constants would
couple two limits that have no reason to move together — a company logo and a
passport photo are different objects — and the coupling would only be
discovered when changing one broke the other. Defaults matter for a second
reason: a missing `.env` entry must not stop `api` booting.

### Schema

`clients` gains `logo_key` (`Text`, nullable) and `logo_updated_at`
(`timestamptz`, nullable), exactly as `candidates` carries `avatar_key` and
`avatar_updated_at`
([candidate.py:157-158](../../../backend/app/models/candidate.py)). One
additive migration; no new table, so no new RLS policy.

`logo_updated_at` exists so the frontend can bust its own image cache without
re-reading the object, which is the same reason the candidate column exists.

### No new environment variable

`R2_ENDPOINT_URL`, `R2_SECRET_ACCESS_KEY` and `R2_BUCKET_NAME` are already set
on the `api` service — that was the second recorded outage, and it is already
fixed. This feature adds no call to any system `api` does not already talk to.
Checked deliberately rather than assumed, because `CLAUDE.md` says to check
exactly this before shipping anything that touches an external system.

## Frontend

New `frontend/app/dashboard/clients/client-logo.tsx`, mirroring
`candidates/candidate-avatar.tsx`: **the mark itself is the control** — a
camera overlay on hover and focus over a real `<input type="file">`, so it
stays keyboard-operable. No separate Upload button.

| File | Change |
|---|---|
| `clients/client-logo.tsx` (new) | The logo contained in a rounded square; initials fallback; upload and delete. |
| `clients/client-panel.tsx` | The logo at the top of the panel, beside the name. |
| `dashboard/clients.ts` | `uploadClientLogo`, `getClientLogo`, `deleteClientLogo`, `clientLogoPath`; `Client` gains `logo_key` and `logo_updated_at`. |
| `dashboard/job-orders-sourcing.tsx` | The client's **name and logo** where it now shows a bare `client_id`. |

Styles go in `dashboard/clients/clients.css`, not `app/app.css` — that file is
at 1507 lines, already past this repo's own 1500-line ceiling.

### Three rules the components must follow

1. **The presigned URL is fetched fresh and never cached beyond the
   component's lifetime.** It expires in 300 seconds: a cached one becomes a
   broken image, and a persisted one is a leaked capability.
2. **The empty state is initials, never a generic building icon.** "MP" for
   Meridian Partners is information; a placeholder glyph looks like a failure
   to load.
3. **`run.client_id` can be null** on the sourcing screen when client
   resolution failed. No request fires for a null id, and the existing
   unresolved notice
   ([job-orders-sourcing.tsx:344](../../../frontend/app/dashboard/job-orders-sourcing.tsx))
   stays exactly as it is.

### The sourcing screen gains a name, not only a picture

Today that screen shows `run.client_id` and nothing else identifying — a UUID.
A logo beside a UUID is a puzzle, so this adds the client's **name** as well.
That is slightly more than "add a picture", and it is intentional: the screen
already claims to tell a recruiter which client the already-submitted exclusion
was applied for, and a UUID does not do that.

## Tests

Mirroring `backend/tests/test_candidate_avatar.py`, which fakes R2 with an
`InMemoryBodyStore` double injected through `app.dependency_overrides`. R2 is
never contacted for real.

- Upload, then GET returns a presigned URL; DELETE, then GET is 404.
- Over `CLIENT_LOGO_MAX_UPLOAD_BYTES` is 413, and the check fires **before**
  the image is decoded.
- A non-image body is 400, not 500.
- A decompression bomb is refused.
- EXIF is stripped: upload an image carrying EXIF, read the stored bytes back
  from the double, assert it is gone.
- **A wide image comes back square and uncropped** — assert the stored image's
  dimensions are square and that its content is letterboxed rather than
  centre-cropped. This is the rule that distinguishes this path from the
  candidate one, so it gets an explicit test.
- The stored object is PNG regardless of the uploaded format, and the padding
  is transparent.
- Another agency's client id is a 404 on all three verbs.
- Deleting when the object store fails leaves `logo_key` intact, so the delete
  can be retried.
- Re-uploading replaces the bytes at the same deterministic key and moves
  `logo_updated_at`.
