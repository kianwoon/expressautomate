# Job order PDF/DOC upload with AI prefill

Adds document upload to the **New job order** dialog: a recruiter can drop in a
PDF or Word file instead of (or alongside) transcribing a phone call, and the
system reads the document, extracts the vacancy fields with the existing email
extraction pipeline, and pre-fills the form for review before saving. The file
stays attached to the saved job order and is downloadable from the detail panel.

## Why

The manual form exists for vacancies taken over the phone or WhatsApp
(`job-order-form.tsx`). A large class of vacancies arrives differently: the
client emails a **job description document** — a PDF or a Word file — and the
recruiter must currently either re-type the whole thing into the free-text
fields (slow, lossy) or forward the document as an email (which the pipeline
treats as a recruitment email and may or may not extract). Uploading the file
directly into the create dialog removes both round-trips.

Prefill is *reviewed*, never trusted blindly: the same anti-fabrication
discipline that governs email extraction (§15) applies. The model quotes the
source text; the extraction is verified deterministically; the recruiter sees
the extracted values in the familiar form and can correct any of them before
Save.

## Data model

New table `opportunity_documents`, one row per uploaded file:

| column | notes |
|---|---|
| `id` (uuid pk) | minted by the upload route |
| `tenant_id` | TenantScoped, composite FK to opportunities below |
| `opportunity_id` | composite FK `(tenant_id, opportunity_id) → opportunities(tenant_id, id)` — created nullable so an upload can precede the opportunity |
| `filename`, `content_type`, `byte_size` | shown back to the recruiter |
| `object_key` | computed R2 key, never client-supplied |
| `extract_state` | `pending` / `extracting` / `extracted` / `failed` / `unreadable` (mirrors CV parse states) |
| `extract_error` | sentence for the panel |
| `prefill` (jsonb) | the extracted values handed to the form (see below) |
| `uploaded_by` | SET NULL on user delete |
| `created_at`, `updated_at` | Timestamps |

RLS: FORCE ROW LEVEL SECURITY with the standard `tenant_isolation` policy,
created in the same migration as the table (boot fails otherwise).

Composite FK is nullable on `opportunity_id` so the file can be stored before
the vacancy exists (the create dialog flow: upload → extract → review → save →
link). The link is written by `create_opportunity` when the form carries
`document_id`. Deleting an opportunity cascades to its documents (composite
FK `ON DELETE CASCADE`).

`prefill` holds the first extracted job's values in the *form's* vocabulary —
`job_title_raw`, `salary_raw`, `working_hours_raw`, etc. — plus `company_name`,
`location`, `duration`, `employment_type`, `job_description`, `requirements`.
Values are `null` when the document did not mention them; no fabricated value
is ever stored.

## Storage

R2, reusing the existing `BodyStore`. New computed key:

```
opportunity_document_key(tenant_id, opportunity_id, document_id, kind)
    → "{tenant_id}/opportunities/{opportunity_id}/documents/{document_id}.{kind}"
```

`kind` is what `sniff` decided from the bytes (`pdf`/`docx`/`doc`), never the
extension. `.doc` (legacy Word) is stored as-is and converted to `.docx`
inside the worker with the existing `cv.convert.maybe_convert` machinery —
identical to the CV path.

Config additions (all `backend/app/core/config.py`):

- `OPPORTUNITY_DOCUMENT_MAX_UPLOAD_BYTES` (default 10 MB)
- `OPPORTUNITY_DOCUMENT_PRESIGNED_URL_TTL_SECONDS` (default 300)
- `OPPORTUNITY_DOCUMENT_EXTRACT_TIMEOUT_SECONDS` (default 300)

No daily quota: job-description uploads are a create-dialog action (one file
per vacancy), not a bulk path like CVs. If abuse shows up later, add a quota
then — the CV quota's `COUNT(*)` pattern is the template.

## API

New router `app/api/opportunity_documents.py`, included **before**
`opportunities.router` in `main.py` (the literal `documents` segment must not
be shadowed by `/opportunities/{opportunity_id}` — the same convention as
`candidate_documents`). Owns:

- `POST /api/opportunities/{opportunity_id}/documents` — attach a file to an
  existing job order (future use; visible/editable guard).
- `POST /api/opportunities/documents` — **the create-dialog path**: upload
  with no opportunity yet. Sniffs bytes, stores to R2, creates the
  `opportunity_documents` row (`opportunity_id NULL`, `extract_state
  pending`), enqueues `extract_opportunity_document`, returns the document
  row. Declared before the `{opportunity_id}` route for the shadowing reason
  above.
- `GET /api/opportunities/documents/{document_id}` — poll for extraction
  state and prefill. Tenant-scoped read; 404 for another agency.
- `GET /api/opportunities/documents/{document_id}/download` — presigned URL
  (only once the document is linked to a visible opportunity).
- `DELETE /api/opportunities/documents/{document_id}` — remove the file and
  row (used by the form's "remove file" control, and the detail panel's for
  the assignee/owner). A linked document requires **edit** rights on its
  vacancy (403 for a read-only share recipient), never merely visibility:
  removing the source file is an edit to the job order.

The create flow, in order:

1. `POST /api/opportunities/documents` (multipart) → 201/202 with the row.
2. Frontend polls `GET /api/opportunities/documents/{id}` until
   `extract_state ∈ {extracted, failed, unreadable}`, capped at 30 polls so a
   row the worker will never resolve does not leak requests forever.
3. On `extracted`, the form's fields are pre-filled from `prefill` and marked
   "read from your file — check before saving". A recruiter's own typing is
   never clobbered: a field already typed by hand is skipped.
4. On Save, `POST /api/opportunities` carries `document_id`; the backend links
   the row (refusing a document already linked to another vacancy) and writes
   the vacancy.

## Worker

`extract_opportunity_document` in `app/workers/opportunity_document_jobs.py`
(the pattern of `cv_jobs.py` — its own module, jobs.py is at the ceiling):

1. Claim the row (`pending → extracting`) under the tenant session.
2. Read bytes from R2 (`get_bytes`).
3. `maybe_convert` `.doc` → `.docx`; `cv.text.extract_text` → source text.
4. Run `ingest.extract(source)` — the **same prompt and verification** as
   email extraction, so the evidence discipline is inherited for free.
5. Take the first extracted job and map `ExtractedJob` → the form's prefill
   vocabulary; write `extract_state = extracted` + `prefill`.
6. Empty text → `unreadable` (like a scanned CV); model failure → `failed`.
   A `.doc` (OLE2) is converted with the CV machinery; on a deployment without
   LibreOffice it parks `unreadable` with the "save as .docx" sentence, never
   stranding the row at `extracting` for `rescan_stuck` to re-enqueue forever.

Registered in `app/workers/settings.py` with a `func(..., name=...)` wrapper
and `OPPORTUNITY_DOCUMENT_EXTRACT_TIMEOUT_SECONDS`. `rescan_stuck` gains a
block that re-enqueues stale `pending`/`extracting` rows via a new
`stalled_opportunity_documents` SECURITY DEFINER resolver (the sweep runs
unscoped; FORCE RLS would otherwise match zero rows — copy
`stalled_candidate_documents`).

## Frontend

`frontend/app/dashboard/job-order-form.tsx`:

- A drop zone at the top of the dialog (mirror `cv-ingest-dialog.tsx`): PDF or
  Word, `accept=".pdf,.docx,.doc,…"`, one file.
- On file chosen → `POST /api/opportunities/documents`, then poll
  `GET /api/opportunities/documents/{id}` every ~2s while
  `extract_state ∈ {pending, extracting}`.
- On `extracted`: fill `fields` from `prefill`; show a note ("Read from
  your_file.pdf — check it, then save."). The title is required, so an
  extraction with no title still disables Save until the recruiter types one.
- On `failed`/`unreadable`: show the sentence; the recruiter can still type by
  hand (the file remains attached for reference) or remove the file.
- On Save: include `document_id` in the `ManualOpportunity` body.

`frontend/app/dashboard/detail-panel.tsx`:

- A "Source file" block when the opportunity payload carries documents: file
  name + size + download link (presigned, fetched on click) + remove for the
  assignee/owner.
- `frontend/app/dashboard/opportunities.ts` `Opportunity` gains
  `documents?: OpportunityDocument[]`.

New `frontend/app/dashboard/opportunity-documents.ts` with
`uploadOpportunityDocument`, `getOpportunityDocument`, `getOpportunityDocumentUrl`,
`deleteOpportunityDocument` (copy the candidate-document helpers' shapes).
New `api.ts` path helpers (they must appear in the route manifest — the
contract test enforces it).

## Serializer

`_payload` in `opportunities.py` gains `documents`: a list of
`{id, filename, content_type, byte_size, extract_state, extract_error,
created_at}` for the row, fetched like `_decoded_codes` — one aggregate query
per page, never one query per row. The list and detail panel read the same
payload, so they cannot disagree.

## Tests

Backend:

- `tests/test_opportunity_documents_api.py` — upload stores/enqueues/sniffs;
  wrong type is 415; other-agency id is 404; poll returns prefill once
  extracted; delete removes bytes then row; download presigns.
- `tests/test_opportunity_document_job.py` — text extraction + LLM prefill
  mapping; empty text → `unreadable`; `.doc` conversion.
- `tests/test_route_manifest.py` — regenerate the manifest.
- Guarded-route test: the new module reads no `Opportunity` directly except
  through the visibility service, so add exemptions only if the structural
  test flags it (and only with a reason).

Frontend:

- `job-order-form.test.tsx` — upload → poll → prefill → save carries
  `document_id`; remove-file; extraction error leaves typing possible.

## Out of scope

- Attaching a document to an *existing* job order from the detail panel
  (the `POST /opportunities/{id}/documents` route is a stub; the UI button is
  not built).
- OCR of scanned job descriptions (off, like CV OCR).
- Multiple documents per job order.
