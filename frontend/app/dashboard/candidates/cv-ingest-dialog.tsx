"use client";

import { useState } from "react";

import { uploadCandidateDocumentNoCandidate } from "../candidates";
import { Dialog } from "../dialog";

/**
 * The drop-a-CV-in path: upload a CV without naming a candidate, and let the
 * backend read its contact details and resolve the person. Mirrors the
 * per-candidate drop-zone (`candidate-cv.tsx`) for the upload mechanics — same
 * drag-and-drop, same file-input-as-the-control, same accept list — but stands
 * alone as a dialog over the list rather than a panel beside a known row.
 *
 * No client-side polling. The ingest job runs in the worker and the resolved
 * candidate appears in the list on the page's next refresh — the caller's
 * `onClose` reloads the list, and the page's existing SSE/poll keeps it fresh.
 * A spinner with no real signal to read would be theatre; an honest "reading in
 * the background, the candidate will appear in the list" is what the upload
 * actually bought.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the
 * dialog, not a list anything is matched against.
 */

const ACCEPT =
  ".pdf,.docx,application/pdf," +
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

type Status =
  | { kind: "idle" }
  | { kind: "uploading" }
  | { kind: "done" }
  | { kind: "error"; message: string };

export function CvIngestDialog({
  onClose,
}: {
  /** Called when the dialog should close. Always reloads the caller's list — a
   *  resolved candidate may have appeared, and a 202 costs nothing to refresh. */
  onClose: () => void;
}) {
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [dragging, setDragging] = useState(false);

  const busy = status.kind === "uploading";

  async function upload(file: File) {
    if (busy) return;
    setStatus({ kind: "uploading" });
    try {
      await uploadCandidateDocumentNoCandidate(file);
      setStatus({ kind: "done" });
    } catch (err) {
      setStatus({
        kind: "error",
        message:
          err instanceof Error ? err.message : "We could not upload that CV just now.",
      });
    }
  }

  function onFileChosen(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // so choosing the same file twice fires onChange again
    if (file) void upload(file);
  }

  function onDrop(event: React.DragEvent) {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) void upload(file);
  }

  // After a successful upload, "Close" returns to the list and reloads it; the
  // resolved candidate appears there once the ingest job finishes. Allowing
  // another upload before closing would queue a second ingest, which is fine —
  // but the done state shows the keep-dropping affordance so a recruiter with a
  // folder of CVs can drop several in succession.
  return (
    <Dialog
      titleId="cv-ingest-title"
      className="dlg-modal-wide"
      onClose={busy ? () => {} : onClose}
      title="Add a candidate from a CV"
    >
      <p className="body" style={{ marginTop: 4 }}>
        Drop in a CV and we will read the person&apos;s contact details, match them to an
        existing candidate or add a new one, then read their work history. PDF or Word.
      </p>

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
        style={{ marginTop: 16 }}
      >
        <label className="cv-trigger">
          <span className="cv-invite">
            {status.kind === "uploading"
              ? "Uploading…"
              : "Drop a CV here, or click to choose one."}
          </span>
          <span className="cv-kinds">PDF or Word</span>
          <input
            className="ca-file"
            type="file"
            accept={ACCEPT}
            onChange={onFileChosen}
            disabled={busy}
            aria-label="Upload a CV to create a candidate"
          />
        </label>
      </div>

      {status.kind === "done" && (
        <p className="body cv-detail" style={{ marginTop: 12 }}>
          CV received. The candidate will appear in the list once their contact details
          are read — usually within a minute. You can drop another in, or close.
        </p>
      )}

      {status.kind === "error" && (
        <p className="body jo-detail-error" role="alert" style={{ marginTop: 12 }}>
          {status.message}
        </p>
      )}

      <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={onClose}
          disabled={busy}
        >
          Close
        </button>
      </div>
    </Dialog>
  );
}
