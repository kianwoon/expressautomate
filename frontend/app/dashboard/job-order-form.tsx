"use client";

import { useEffect, useId, useRef, useState } from "react";

import { ClientSearch, type ClientMatch } from "./client-search";
import { ApiError } from "./candidates";
import { Dialog } from "./dialog";
import {
  deleteOpportunityDocument,
  getOpportunityDocument,
  uploadOpportunityDocument,
  type OpportunityDocument,
} from "./opportunity-documents";
import { createOpportunity, type ManualOpportunity } from "./opportunity-actions";

/**
 * A job order that never arrived as an email.
 *
 * Some vacancies are taken over the phone or on WhatsApp while the client is
 * still talking, and until this form existed the only way one entered the
 * system was through a mailbox. That made every hand-taken job order invisible
 * to the very list that is supposed to be all of them.
 *
 * Some arrive the other way: the client emails a **job-description document** —
 * a PDF or a Word file — and the recruiter should not have to re-type the
 * whole thing. Drop the file here and the system reads it in the background,
 * then pre-fills the fields below for review. The file stays attached to the
 * saved job order and is downloadable from the detail panel.
 *
 * The prefill is *reviewed*, never trusted blindly: the same anti-fabrication
 * discipline that governs email extraction applies. A field the document never
 * mentioned stays empty — nothing is invented — and the title is still the one
 * required field, so an extraction with no title cannot be saved until the
 * recruiter types one.
 *
 * Every typed field is free text and every one is optional but the title,
 * because the recruiter is transcribing what they were told. Turning "6k neg."
 * into a number here would be the fabrication §15 forbids, and normalising it
 * is the extraction pipeline's job — which never runs on these rows.
 *
 * There is no assignee control, and its absence is deliberate: the server
 * assigns the row to whoever created it. You typed it in, so it is yours,
 * not the client account holder's.
 *
 * allow-hardcode: the strings below are user-facing labels and copy rendered
 * to the page, not a list anything is matched against.
 */

/** The kinds the server will accept, stated here only as the file chooser's
 *  filter — the server sniffs the bytes and is the one that decides, so a file
 *  that slips past this is refused there with a sentence of its own rather
 *  than silently accepted. `.doc` is included because the backend accepts and
 *  converts legacy Word. */
const ACCEPT =
  ".pdf,.docx,.doc,application/pdf,application/msword," +
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

/** How often to re-read the document while the extraction is in flight. An
 *  extraction is a model call behind a queue, so it finishes on its own
 *  schedule and nothing pushes the result here. Two seconds is quick enough
 *  that a recruiter who just dropped a file sees the fields fill in without
 *  wondering whether to reload. The poll exists only while a document is
 *  actually extracting; see the effect below. */
const POLL_MS = 2000;

/** How many polls before the form stops asking and settles for "still
 *  reading". An extraction is a model call that can take a while, but a poll
 *  that never gives up is a dialog that leaks requests forever on a row the
 *  worker will never resolve. After the cap the document stays attached and
 *  the recruiter can save with what they typed — the file travels with the
 *  row either way, and its values were never a promise. */
const POLL_CAP = 30;

/** Trimmed, and empty becomes `null` rather than `""`.
 *
 * The database column is nullable and "" is not a value anyone typed — it is
 * a field they left alone. Sending the empty string would make a job order
 * with a blank location sort and search differently from one with no location
 * recorded, on a distinction the recruiter never made. */
function orNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/** One labelled free-text field. A component rather than nine copies of the
 *  same three elements, so the label and the input cannot drift apart.
 *
 *  `placeholder` is a concrete, realistic example of what goes in the field —
 *  never an instruction like "enter job title" — because the recruiter is
 *  transcribing a phone call at speed and a real example reads faster than a
 *  command does. It is a placeholder only: it never becomes the value and
 *  never gets submitted. */
function Field({
  label,
  value,
  onChange,
  multiline,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  multiline?: boolean;
  placeholder?: string;
}) {
  const id = useId();
  return (
    <div className="jo-form-field">
      <label htmlFor={id}>{label}</label>
      {multiline ? (
        <textarea
          id={id}
          className="jo-search"
          rows={3}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          id={id}
          className="jo-search"
          type="text"
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
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

/** The fields the prefill may touch, keyed by form field name. A value the
 *  document never mentioned comes back as null and is ignored — the form only
 *  ever overwrites a field the recruiter has not typed in, so a half-made
 *  transcription is not clobbered by the file's answer to the same question. */
const PREFILL_FIELDS = [
  "job_title_raw",
  "company_name_raw",
  "location_raw",
  "salary_raw",
  "working_hours_raw",
  "duration_raw",
  "employment_type",
  "job_description",
  "requirements",
] as const;

export function JobOrderForm({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  /** Handed the id of the row that was just created. The caller reads it back
   *  and puts it in the list — this form does not know what the list holds. */
  onCreated: (id: string) => Promise<void> | void;
}) {
  const titleId = useId();
  const [client, setClient] = useState<ClientMatch | null>(null);
  const [fields, setFields] = useState({
    job_title_raw: "",
    company_name_raw: "",
    location_raw: "",
    salary_raw: "",
    working_hours_raw: "",
    duration_raw: "",
    employment_type: "",
    job_description: "",
    requirements: "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [document, setDocument] = useState<OpportunityDocument | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Which fields the extraction filled and the recruiter has not edited since.
  // The form only auto-fills an empty field; once typed in, a recruiter's own
  // word wins even when a later poll delivers a value for the same field.
  const untouchedByRecruiter = useRef<Set<string>>(new Set(PREFILL_FIELDS));

  const set = (key: keyof typeof fields) => (next: string) => {
    untouchedByRecruiter.current.delete(key);
    setFields((current) => ({ ...current, [key]: next }));
  };

  // The one thing required. A row with no title is not a job order anybody
  // could work from, and it would sit in the list as a blank line.
  const ready = fields.job_title_raw.trim().length > 0;

  const extracting =
    document?.extract_state === "pending" || document?.extract_state === "extracting";

  // Poll the extraction until it lands in a terminal state, or the cap runs
  // out. The poll exists only while a document is in flight; see POLL_MS.
  useEffect(() => {
    if (!document || !extracting) return;
    const docId = document.id;
    let stopped = false;
    let polls = 0;
    async function tick() {
      if (polls >= POLL_CAP) return; // still attached, recruiter can save
      polls += 1;
      try {
        const next = await getOpportunityDocument(docId);
        if (stopped) return;
        applyDocument(next);
        if (next.extract_state === "extracted") {
          setDocument(next);
          prefillFrom(next);
        } else if (next.extract_state === "failed" || next.extract_state === "unreadable") {
          setDocument(next);
        } else {
          window.setTimeout(tick, POLL_MS);
        }
      } catch (err) {
        // A 404 means the document row is gone — deleted server-side or never
        // existed — and asking again can only get the same answer. Stop
        // quietly; the recruiter can save without it. Anything else is a
        // transient network failure, not a terminal state, so keep polling.
        if (!stopped && !(err instanceof ApiError && err.status === 404)) {
          window.setTimeout(tick, POLL_MS);
        }
      }
    }
    void tick();
    return () => {
      stopped = true;
    };
    // `document` changes identity on every poll read-back, so the effect must
    // only re-run when the *id* or terminality changes — otherwise each poll
    // restarts the poll. Deliberately not `document` in the deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [document?.id, extracting]);

  /** Merge the latest row into state without clobbering a recruiter's edits. */
  function applyDocument(next: OpportunityDocument) {
    setDocument((current) => {
      if (!current) return next;
      return { ...next, prefill: current.prefill ?? next.prefill };
    });
  }

  /** Copy the extraction's answers into fields the recruiter has not touched. */
  function prefillFrom(next: OpportunityDocument) {
    const prefill = next.prefill ?? {};
    setFields((current) => {
      const merged = { ...current };
      for (const key of PREFILL_FIELDS) {
        if (!untouchedByRecruiter.current.has(key)) continue;
        const value = prefill[key];
        if (typeof value === "string") merged[key] = value;
      }
      return merged;
    });
    // The client field is a search box over clients, not free text; the
    // extraction's company name flows into `company_name_raw` above and travels
    // with the row when nothing matches — the picker itself is left empty so a
    // recruiter still has to choose (or clear) the client consciously.
  }

  async function upload(file: File) {
    if (uploading || extracting) return;
    setUploadError(null);
    setUploading(true);
    // Reset the "which fields the extraction may fill" set NOW, before the
    // request, not after it resolves. A recruiter who starts typing while the
    // upload is in flight has already decided their own word wins — resetting
    // after the response would silently re-arm the extraction to clobber it.
    untouchedByRecruiter.current = new Set(PREFILL_FIELDS);
    try {
      const created = await uploadOpportunityDocument(file);
      if (created.extract_state === "failed") {
        // The route tells us the job never got queued (a Redis outage). The
        // file exists but nothing will read it — "it will prefill shortly"
        // would be a promise nothing is keeping. Say the real sentence instead,
        // and let the recruiter either remove the file or type by hand.
        setDocument(created);
        setUploadError(
          created.extract_error ??
            "We could not upload that file just now. Try again in a few minutes.",
        );
        return;
      }
      setDocument(created);
    } catch (err) {
      setUploadError(
        err instanceof Error ? err.message : "We could not upload that file just now.",
      );
    } finally {
      setUploading(false);
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

  async function removeDocument() {
    if (!document) return;
    const previous = document;
    setDocument(null);
    setUploadError(null);
    try {
      await deleteOpportunityDocument(previous.id);
    } catch {
      // The removal failed server-side; the file row is still there. Restore
      // the attachment so the recruiter does not believe it is gone while the
      // server still holds it — a dangling row with no reference is worse than
      // one the recruiter can see and try to remove again.
      setDocument(previous);
      setUploadError("We could not remove that file just now. Try again.");
    }
  }

  async function save() {
    if (!ready || saving) return;
    setSaving(true);
    setError(null);
    const body: ManualOpportunity = {
      client_id: client ? client.id : null,
      company_name_raw: orNull(fields.company_name_raw),
      job_title_raw: orNull(fields.job_title_raw),
      location_raw: orNull(fields.location_raw),
      salary_raw: orNull(fields.salary_raw),
      working_hours_raw: orNull(fields.working_hours_raw),
      duration_raw: orNull(fields.duration_raw),
      employment_type: orNull(fields.employment_type),
      job_description: orNull(fields.job_description),
      requirements: orNull(fields.requirements),
      document_id: document ? document.id : null,
    };
    const result = await createOpportunity(body);
    if (!result.ok) {
      // The dialog stays open with everything still in it. A recruiter who has
      // just transcribed a phone call must not have to transcribe it twice.
      setSaving(false);
      setError(result.message);
      return;
    }
    await onCreated(result.created.id);
    onClose();
  }

  return (
    <Dialog title="New job order" titleId={titleId} onClose={onClose} className="jo-form-dialog">
      <p className="body jo-form-intro">
        For a vacancy taken over the phone or on WhatsApp. Type what you were told — or drop in
        the client&apos;s PDF or Word file and we will read the details for you to check.
      </p>

      <div
        className="jo-doc-drop"
        data-drop={dragging ? "yes" : undefined}
        data-busy={uploading || extracting ? "yes" : undefined}
        data-has={document ? "yes" : undefined}
        onDragOver={(event) => {
          event.preventDefault();
          if (!uploading && !extracting) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <label className="jo-doc-trigger">
          <span className="cv-invite">
            {uploading
              ? "Uploading…"
              : extracting
                ? "Reading the file…"
                : document
                  ? document.filename
                  : "Drop a job description here, or click to choose one."}
          </span>
          <span className="cv-kinds">PDF or Word</span>
          <input
            className="ca-file"
            type="file"
            accept={ACCEPT}
            onChange={onFileChosen}
            disabled={uploading || extracting}
            aria-label="Upload a job description file"
          />
        </label>
        {document && (extracting || document.extract_state === "extracted") && (
          <div className="jo-doc-meta">
            <span className="jo-doc-size">{formatSize(document.byte_size)}</span>
            {extracting ? (
              <span className="cv-pip" aria-label="Reading the file" />
            ) : (
              <span className="jo-doc-ok">Read — check the fields below</span>
            )}
            <button type="button" className="jo-doc-remove" onClick={() => void removeDocument()}>
              Remove
            </button>
          </div>
        )}
        {uploadError && (
          <p className="body jo-doc-error" role="alert">
            {uploadError}
          </p>
        )}
      </div>

      <div className="jo-form-grid">
        <Field
          label="Job title"
          value={fields.job_title_raw}
          onChange={set("job_title_raw")}
          placeholder="Warehouse assistant"
        />
        <ClientSearch
          value={client}
          onChange={setClient}
          label="Client"
          placeholder="Sunrise Logistics Pte Ltd"
          onQueryChange={set("company_name_raw")}
        />
        <Field
          label="Location"
          value={fields.location_raw}
          onChange={set("location_raw")}
          placeholder="Tuas"
        />
        <Field
          label="Pay"
          value={fields.salary_raw}
          onChange={set("salary_raw")}
          placeholder="$2,800/month"
        />
        <Field
          label="Working hours"
          value={fields.working_hours_raw}
          onChange={set("working_hours_raw")}
          placeholder="Mon–Fri, 9am–6pm"
        />
        <Field
          label="How long it runs"
          value={fields.duration_raw}
          onChange={set("duration_raw")}
          placeholder="6-month contract"
        />
        <Field
          label="Kind of work"
          value={fields.employment_type}
          onChange={set("employment_type")}
          placeholder="Full-time"
        />
      </div>

      <Field
        label="What the job involves"
        value={fields.job_description}
        onChange={set("job_description")}
        placeholder="Picking and packing orders at a Jurong East warehouse. Some heavy lifting."
        multiline
      />
      <Field
        label="What they are looking for"
        value={fields.requirements}
        onChange={set("requirements")}
        placeholder="At least 1 year warehouse experience. Able to work weekends."
        multiline
      />

      {document?.extract_state === "unreadable" && (
        <p className="body jo-doc-note" role="status">
          We could not read text out of {document.filename} — it may be a scan. The file is still
          attached for reference; type the vacancy in by hand, or remove it.
        </p>
      )}

      {error && (
        <p className="jo-form-error" role="alert">
          {error}
        </p>
      )}

      <div className="jo-form-actions">
        <button type="button" className="btn btn-secondary" onClick={onClose} disabled={saving}>
          Cancel
        </button>
        <button type="button" className="btn btn-primary" onClick={() => void save()} disabled={!ready || saving}>
          {saving ? "Saving" : "Save job order"}
        </button>
      </div>
    </Dialog>
  );
}
