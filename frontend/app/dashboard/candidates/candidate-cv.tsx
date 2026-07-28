"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { Candidate, CandidateDocument, ParseState } from "../candidates";
import {
  deleteCandidateDocument,
  getCandidateDocumentUrl,
  uploadCandidateDocument,
} from "../candidates";

/**
 * The candidate's CV: drop one here, and watch it being read.
 *
 * The upload is the easy half. The hard half is the six states a document can
 * be in afterwards, because a recruiter reads all of them as one thing —
 * "did it work?" — and only two of the six have the same answer.
 *
 * So the states are written out one by one in `describe` rather than mapped
 * through a table of severities. `unreadable` is the one that matters most: a
 * scanned CV is the single most common thing a recruitment agency has on file,
 * and a message that says only "we could not read this" is indistinguishable
 * from a product that is broken. It has to name the cause, say what can be
 * done now, and say that the gap is being closed.
 *
 * The house style is `candidate-avatar.tsx`'s: the control is the thing
 * itself. There is no "Choose file" button beside an "Upload" button — the
 * panel is the drop target and it holds a real, focusable file input, so the
 * whole of it is operable from a keyboard. The per-document controls are on
 * the document, revealed by hover *and* by focus, and never taken out of the
 * tab order.
 *
 * allow-hardcode: every string below is UI copy shown to a recruiter — the
 * craft of this component is precisely the wording of the six states. None of
 * it is matched against, parsed, or used to decide anything; `parse_state`
 * from the server is the only signal, and these are its translations.
 */

/**
 * How often to re-read the candidate while a CV is still being read.
 *
 * A parse is a model call behind a queue, so it finishes on its own schedule
 * and nothing pushes the result here — the dashboard's event stream carries
 * mail, not this. Six seconds is slow enough that an open panel is not a load
 * generator and quick enough that a recruiter who uploaded a CV sees the roles
 * appear rather than wondering whether to reload the page. The poll exists
 * only while a document is actually in flight; see the effect below.
 */
const POLL_MS = 6000;

/** The two kinds the server will accept, stated here only as the file
 *  chooser's filter — the server sniffs the bytes and is the one that decides,
 *  so a file that slips past this is refused there with a sentence of its own
 *  rather than silently accepted. */
const ACCEPT =
  ".pdf,.docx,application/pdf," +
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

const IN_FLIGHT: ParseState[] = ["pending", "parsing"];

function isInFlight(document: CandidateDocument): boolean {
  return IN_FLIGHT.includes(document.parse_state);
}

/** Bytes as a person would say them. Binary units, one decimal past a
 *  megabyte, because "1258291 bytes" tells a recruiter nothing about whether
 *  their file is too big. */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} bytes`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

type Tone = "working" | "good" | "warn" | "bad";

type Description = {
  tone: Tone;
  /** One line, always. What happened. */
  headline: string;
  /** The rest of it: the cause, and what to do about it. Absent when the
   *  headline is the whole truth. */
  detail?: string;
  /** Only a state that might come out differently next time offers this. */
  retry?: boolean;
};

/**
 * What one parse state means, in the words the recruiter needs.
 *
 * Written as six branches rather than a lookup, because the six differ in more
 * than their wording — one offers a retry, one is permanent, one is a success
 * that produced nothing, and confusing any of them with another is the failure
 * this component exists to prevent.
 */
function describe(document: CandidateDocument): Description {
  switch (document.parse_state) {
    case "pending":
      return { tone: "working", headline: "Queued for reading." };
    case "parsing":
      return { tone: "working", headline: "Reading the CV now…" };
    case "parsed":
      return {
        tone: "good",
        headline: "Read. Anything it found is in the work history below, waiting to be checked.",
      };
    case "empty":
      // A success by every technical measure, and useless to the person who
      // uploaded it. Saying "read" alone here would be a silent no-op dressed
      // up as a result.
      return {
        tone: "warn",
        headline: "We read this file, but nothing in it survived checking.",
        detail:
          "Nothing has been added to their history. That usually means the CV is laid out in a " +
          "way we could not follow yet — the roles can be typed in by hand below.",
      };
    case "unreadable":
      // The most common failure an agency will meet, and the one where a bare
      // refusal does the most damage: a scan looks, to the person holding it,
      // exactly like every other CV they have ever uploaded.
      return {
        tone: "warn",
        headline: "This file looks like a scan rather than a text document.",
        detail:
          "There are no words inside it for us to read — only a picture of them, which is what a " +
          "scanner or a photographed page produces. The file is kept and can still be opened and " +
          "sent on, and the roles can be typed into the work history below meanwhile. Reading " +
          "scanned CVs is coming.",
      };
    case "failed":
      return {
        tone: "bad",
        headline: "Something went wrong reading this CV.",
        // The server's own sentence where it left one — it knows which step
        // broke, and no message written here could.
        detail: document.parse_error ?? "Nothing was changed on the candidate.",
        retry: true,
      };
  }
}

/** The note about what the model proposed and we threw away.
 *
 * Rendered on a *successful* parse, which is exactly when it is easy to
 * forget: a CV listing five jobs that yields three roles reads as a parser
 * that cannot count, unless the two that were dropped are accounted for. */
function droppedNote(document: CandidateDocument): string | null {
  if (!document.dropped_count) return null;
  const many = document.dropped_count !== 1;
  const what = `${document.dropped_count} ${many ? "entries were" : "entry was"} left out`;
  return document.dropped_reason ? `${what}: ${document.dropped_reason}` : `${what}.`;
}

function DocumentRow({
  candidateId,
  document,
  busy,
  onRemove,
  onRetry,
}: {
  candidateId: string;
  document: CandidateDocument;
  busy: boolean;
  onRemove: () => void;
  /** Re-uploading is the retry: there is no endpoint that re-queues an
   *  existing row, and inventing one on the client would be a button that
   *  lies. This just puts the recruiter back at the file chooser. */
  onRetry: () => void;
}) {
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const described = describe(document);
  const dropped = document.parse_state === "parsed" ? droppedNote(document) : null;

  /** The URL is fetched on the click rather than held on the row: it expires
   *  in minutes, so a link rendered when the panel opened would be dead by the
   *  time anybody used it. */
  async function open() {
    if (opening) return;
    setOpening(true);
    setError(null);
    try {
      const link = await getCandidateDocumentUrl(candidateId, document.id);
      window.open(link.url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not open that file just now.");
    } finally {
      setOpening(false);
    }
  }

  return (
    <li className="cv-doc" data-tone={described.tone}>
      <div className="cv-doc-line">
        <span className="cv-doc-name" title={document.filename}>
          {document.filename}
        </span>
        <span className="cv-doc-size">{formatSize(document.byte_size)}</span>
        <div className="cv-doc-tools">
          <button
            type="button"
            className="ch-tool"
            onClick={() => void open()}
            disabled={opening}
            aria-label={`Open ${document.filename}`}
          >
            {opening ? "Opening…" : "Open"}
          </button>
          {described.retry && (
            <button
              type="button"
              className="ch-tool"
              onClick={onRetry}
              disabled={busy}
              aria-label={`Upload ${document.filename} again`}
            >
              Try again
            </button>
          )}
          <button
            type="button"
            className="ch-tool ch-tool-remove"
            onClick={onRemove}
            disabled={busy}
            aria-label={`Remove ${document.filename}`}
          >
            Remove
          </button>
        </div>
      </div>

      {/* Polite rather than assertive: the state changes underneath a recruiter
          who is reading something else on the panel, and a parse finishing is
          news worth hearing at the next pause, not an interruption. */}
      <p className="cv-state" aria-live="polite">
        {isInFlight(document) && <span className="cv-pip" aria-hidden="true" />}
        {described.headline}
      </p>
      {described.detail && <p className="cv-detail">{described.detail}</p>}
      {dropped && <p className="cv-detail cv-dropped">{dropped}</p>}
      {error && (
        <p className="cv-detail cv-error" role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

export function CandidateCv({
  row,
  onChanged,
}: {
  row: Candidate;
  /** Refetch this candidate. An upload adds a document; a finished parse adds
   *  roles and can move the derived title, employer and years above — all of
   *  which live on the record this panel is drawing. */
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const documents = row.documents;
  const waiting = (documents ?? []).some(isInFlight);

  // `onChanged` is a prop and may be a fresh closure on every render of the
  // parent, so it is held in a ref rather than named as a dependency: listing
  // it would tear down and rebuild the interval on every poll, which is a way
  // of never quite firing it.
  const refetch = useRef(onChanged);
  refetch.current = onChanged;

  // Only while something is actually being read. A panel sitting open on a
  // candidate whose CVs are all done asks nothing of the server at all.
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => refetch.current(), POLL_MS);
    return () => clearInterval(timer);
  }, [waiting]);

  const upload = useCallback(
    async (file: File) => {
      if (busy) return;
      setBusy(true);
      setError(null);
      try {
        await uploadCandidateDocument(row.id, file);
        onChanged();
      } catch (err) {
        // Too big, wrong kind, or today's uploads used up — the server wrote a
        // sentence for each, and `readError` has already unwrapped it.
        setError(err instanceof Error ? err.message : "We could not upload that CV just now.");
      } finally {
        setBusy(false);
      }
    },
    [busy, onChanged, row.id],
  );

  function onFileChosen(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // so choosing the same file twice fires onChange again
    if (file) void upload(file);
  }

  function onDrop(event: React.DragEvent) {
    event.preventDefault();
    setDragging(false);
    // A drag can carry a folder, a row of text, anything at all. Only a file
    // is worth sending; what kind of file it is, the server decides by reading
    // the bytes rather than trusting anything said about them here.
    const file = event.dataTransfer.files?.[0];
    if (file) void upload(file);
  }

  async function remove(document: CandidateDocument) {
    if (busy) return;
    if (
      !window.confirm(
        `Remove ${document.filename}? The roles it produced stay on the candidate. This cannot be undone.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await deleteCandidateDocument(row.id, document.id);
      onChanged();
      // The button just pressed leaves the document with it. Left alone, focus
      // falls to the body and a keyboard restarts at the top of the page —
      // hand it to the one control on this section that is always here.
      fileRef.current?.focus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not remove that CV just now.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="cv-section" aria-label="CV">
      <div className="ch-head">
        <span className="row-k">CV</span>
      </div>

      <div
        className="cv-drop"
        data-drop={dragging ? "yes" : undefined}
        data-busy={busy ? "yes" : undefined}
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <label className="cv-trigger">
          <span className="cv-invite">
            {busy ? "Uploading…" : "Drop a CV here, or click to choose one."}
          </span>
          <span className="cv-kinds">PDF or Word</span>
          {/* Visually hidden but not `display: none`: it stays in the tab
              order, so the drop panel is reachable and operable without a
              mouse, and the panel lights up from `:focus-within`. */}
          <input
            className="ca-file"
            ref={fileRef}
            type="file"
            accept={ACCEPT}
            onChange={onFileChosen}
            disabled={busy}
            aria-label={`Upload a CV for ${row.full_name}`}
          />
        </label>
      </div>

      {documents === undefined ? (
        // Absent is not empty. A row that arrived from the table has never
        // been asked about its uploads, and "No CV yet" would be a claim about
        // the candidate rather than about what has loaded.
        <p className="body muted ch-empty">Loading their CVs…</p>
      ) : documents.length > 0 ? (
        <ul className="cv-list">
          {documents.map((document) => (
            <DocumentRow
              key={document.id}
              candidateId={row.id}
              document={document}
              busy={busy}
              onRemove={() => void remove(document)}
              onRetry={() => fileRef.current?.click()}
            />
          ))}
        </ul>
      ) : null}

      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
