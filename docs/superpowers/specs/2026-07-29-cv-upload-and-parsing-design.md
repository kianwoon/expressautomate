# CV upload and parsing

Decided 2026-07-29. Backend and UI. Piece 2 of
[work history and sourcing](2026-07-28-candidate-sourcing-decomposition.md).

Piece 1 gave a candidate a list of roles and a recruiter a way to type them.
Nobody types twenty roles. This is the path that fills the table: a recruiter
uploads a CV, a model reads it, and the roles it finds arrive as proposals for
a person to confirm.

It is also the first time this product asks a model to read a document about a
named human being, so the provenance rules matter more here than anywhere else
so far.

## Decisions

| Question | Answer |
|---|---|
| How a CV becomes readable | Text extracted locally, then parsed — never handed whole to a multimodal model |
| What is kept | The original file **and** the extracted text, under per-tenant retention |
| What is parsed | Roles and skills. Not name, email or phone |
| A parsed role that matches a typed one | Attaches its evidence to the existing row; no second row |
| A parsed role that matches nothing | Inserts `unconfirmed`, carrying its `extraction_id` |
| A scanned, text-free CV | `unreadable`, said plainly, never retried |

## Why the text is extracted locally

A multimodal model would read scanned CVs and need no parsing dependency. It
would also destroy the mechanism this codebase relies on to stay honest.

`extract.py` verifies that every span a model quotes **actually exists in the
source text**, and escalates from the fast model to the strong one when a span
does not check out (`app/services/ingest/evidence.py`). With no source text
there is nothing to verify against, and provenance collapses to "the model said
so" — for claims about a real person's employment history.

So: `pypdf` and `python-docx` turn the file into text, and the existing
pipeline runs on that text unchanged. A scanned CV is told plainly that it
cannot be read. That is a real gap, and the honest fallback is a second pass
using a multimodal model later, kept visibly distinct because its rows would
carry weaker provenance than these.

Both libraries are new — nothing in this repo reads documents today.

## Storage

**`candidate_documents`**, inheriting `TenantScoped`, with the composite
`(tenant_id, candidate_id)` foreign key `candidate_roles` already uses.

| Column | Notes |
|---|---|
| `filename`, `content_type`, `byte_size` | What the recruiter uploaded, shown back to them |
| `object_key` | The original file in R2 |
| `text_key`, `text_chars` | The extracted text, also in R2 |
| `parse_state` | `pending` \| `parsing` \| `parsed` \| `unreadable` \| `failed` |
| `parse_error` | A sentence the recruiter can act on |
| `extraction_id` | Nullable FK to `Extraction` |
| `uploaded_by`, timestamps | As elsewhere |

Both keys begin `{tenant_id}/` — `{tenant_id}/candidates/{candidate_id}/cv/{document_id}`
and the same with `.txt`. That prefix is the one a tenant erasure sweep would
purge by, the convention `body_key` and `avatar_key` already keep.

### Why the extracted text is stored, not just used

An evidence span is a character offset into that text. Throw it away and every
span in the system becomes unverifiable after the fact, and a `pypdf` upgrade
that changes whitespace would silently invalidate spans nobody could re-check.
`text_chars` gives the cheap guard: a span running past the end is wrong before
anyone reads it.

### Why `unreadable` and `failed` are separate

A scanned image-only PDF has no text and never will. An LLM timeout is a bad
minute. Collapsing them means either retrying forever against a scan or
abandoning a CV over a blip. This is the same line `BodyStoreMisconfigured`
already draws against transient storage failures: a condition that answers the
same way forever is not an error to retry, it is a fact to report.

## Parsing

An arq job, `parse_candidate_cv(ctx, *, tenant_id, candidate_id, document_id)`,
shaped like `extract_email` (`app/workers/jobs.py:701`) with the tenant in the
payload as every job here carries it.

1. Read the file from R2 and extract text by **sniffed** type, not by the
   filename's extension.
2. No text, or whitespace only → `unreadable`, with a sentence naming the
   likely cause. No retry.
3. Store the text, record `text_chars`, truncate to the configured character
   cap so token spend is bounded by configuration rather than by whatever
   somebody uploaded.
4. Two passes, reusing `extract.py:_attempts()`: the fast model at low
   reasoning effort, escalating to the strong model at high effort **only**
   when a span fails to verify or confidence is below the bar (§32).
5. Verify every evidence span against the stored text before trusting any of
   it. A span that is not really there is what triggers the escalation.
6. Write `Extraction` and `ExtractionEvidence`, then match, then insert.

**The model reports the precision it saw.** "Mar 2019" is month precision, and
no day is invented — §15 expressed in the prompt rather than repaired
afterwards.

### Matching, so a CV does not duplicate a career

A parsed role matches an existing one when `employer_normalized` is equal and
the date ranges overlap. On a match the evidence attaches to the row already
there: the CV corroborates what the recruiter typed, and no second row appears.
Only genuinely new roles insert.

Without this, uploading a CV for an existing candidate floods the timeline with
near-copies to dismiss one at a time, and recruiters simply stop uploading CVs.

### Skills need two columns they do not have

`candidate_skills` predates all of this and has no `source` or `status`. For a
parsed skill to arrive unconfirmed like a parsed role, that table needs both,
by the same migration reasoning as `candidate_roles`. The alternative is
model-derived skills landing as established fact with no human in the loop,
which contradicts every other decision here.

### Replay must be free

The job is idempotent on `document_id`: a document already `parsed` returns
immediately, and inserts are keyed so a retry is a no-op rather than a second
career.

## API

A new file, `app/api/candidate_documents.py` — `candidates.py` is 846 lines
against a 1500-line ceiling.

- `POST /api/candidates/{id}/documents` — multipart. Size capped **before** the
  body is fully read, type sniffed from magic bytes, key derived server-side
  from the authenticated tenant. Stores the file, inserts `pending`, enqueues,
  answers **202**.
- `GET /api/candidates/{id}/documents/{doc_id}/download` — a short-TTL
  presigned URL for the original. This is the route that lets a recruiter
  forward the real CV to a client.
- `DELETE /api/candidates/{id}/documents/{doc_id}` — file, text and row.
- Documents are embedded in the candidate GET, like roles.

**Deleting a document does not delete the roles it produced.** A human may have
confirmed them by then, and they are a person's career rather than an artefact
of a file. The `extraction_id` survives as provenance.

Errors: 413 too large, 415 unsupported type, 503 when storage is misconfigured
(the handler exists), 404 — never 403 — for another agency's candidate.

### The enqueue that cannot fail loudly

`enqueue()` returns a bool and never raises (`app/workers/queue.py`). If Redis
is down the upload succeeds and the document sits in `pending` forever with
nothing to move it. On a false return the row goes to `failed` with a message
saying it can be retried, rather than pretending it is queued.

Progress reaches the browser over the existing live-events channel rather than
by polling — the mechanism added in `27ee802`, which exists to tell the
dashboard something changed instead of making it ask.

## UI

Most of it already exists. Piece 1 shipped the unconfirmed row styling — amber
rail, confirm and reject — with the buttons disabled because nothing produced
such rows. Piece 2 turns them on, which means it must add the endpoints they
call: `POST /api/candidates/{id}/roles/{role_id}/confirm` and `/reject`. That
is work piece 1 deferred, and it belongs here.

Upload sits beside the timeline in the avatar's house style: drop a file on it
or click it, no pair of grey buttons. Parse state renders inline — reading it,
could not read it, failed with a retry.

**An unconfirmed role can show the line from the CV that produced it**, with
its span verified against the stored text. That is the whole §15 apparatus
finally visible to a recruiter: not "the AI says she worked at Parkway
Shenton", but "the CV says this, here".

## Tests

1. Agency A cannot read, download or delete Agency B's documents.
2. Oversized → 413; unsupported type → 415; a file whose extension lies about
   its bytes → 415.
3. A scanned, text-free PDF → `unreadable`, and is not retried.
4. A fabricated evidence span forces escalation; one that still fails after
   escalation does not become a role.
5. A parsed role matching an existing one attaches evidence and creates no
   second row.
6. A genuinely new role inserts `unconfirmed` with its `extraction_id`.
7. Replaying the job inserts nothing twice.
8. A failed enqueue leaves the document `failed`, not `pending`.
9. Deleting a document leaves the roles it produced.
10. Skills dedupe on `skill_normalized`.
11. RLS enforced on `candidate_documents`, and on the amended
    `candidate_skills`.

## Out of scope

CVs arriving as email attachments (piece 3 — attachments are still never
downloaded, confirmed at `app/workers/jobs.py:98`), spreadsheet import of
history (piece 4), and any ranking of candidates against a job order (piece 5).
Parsing a candidate's name, email or phone is deliberately excluded: those are
the identity keys, and a parsed value that disagrees with the record could
split or merge two real people.
