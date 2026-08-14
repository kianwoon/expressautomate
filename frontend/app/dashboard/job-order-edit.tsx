"use client";

import { useId, useState } from "react";

import { updateOpportunity, type Opportunity, type OpportunityUpdate } from "./opportunities";

/**
 * The detail panel's edit mode: the job order's own fields, editable.
 *
 * The read-only panel shows what the email said; this is where a recruiter
 * corrects it — a salary figure, a location, the requirements. Every field is
 * free text and every one can be cleared back to "not mentioned", because the
 * recruiter is transcribing a correction to what they were told. Normalising
 * "6k neg." into a number is the extraction pipeline's job and never runs on a
 * human-typed value; the backend's PATCH re-derives the structured salary
 * range (min, max, currency, period) from the raw string the same way the
 * email pipeline does — so a corrected "Pay" keeps the row sortable without a
 * second field to keep in step.
 *
 * Only the fields that actually changed are sent. The backend records a
 * human-correction override for exactly those fields, so a later replay of the
 * source email never clobbers a fix — and a form that echoed everything back
 * would freeze every field from later replays for a decision nobody made.
 *
 * The save answers with the full row in the list's shape, and the caller
 * swaps it into the list and the panel, so what was typed appears immediately
 * and a colleague's next poll agrees.
 *
 * allow-hardcode: the strings below are user-facing labels and copy rendered
 * to the page, not a list anything is matched against.
 */

/** Trimmed, and empty becomes `null` rather than `""` — a cleared field is
 *  "not mentioned" again, which is the value the panel renders as such. */
function orNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

export function JobOrderEdit({
  row,
  onSaved,
  onCancel,
}: {
  row: Opportunity;
  /** Handed the freshly-read row the server just saved. The caller swaps it
   *  into the list and the panel; this form does not know what the list holds. */
  onSaved: (row: Opportunity) => void;
  onCancel: () => void;
}) {
  const [fields, setFields] = useState({
    company_name_raw: row.company_name_raw ?? "",
    job_title_raw: row.job_title_raw ?? "",
    location_raw: row.location_raw ?? "",
    salary_raw: row.salary_raw ?? "",
    working_hours_raw: row.working_hours_raw ?? "",
    duration_raw: row.duration_raw ?? "",
    employment_type: row.employment_type ?? "",
    job_description: row.job_description ?? "",
    requirements: row.requirements ?? "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (key: keyof typeof fields) => (next: string) => {
    setFields((current) => ({ ...current, [key]: next }));
  };

  /** The fields whose stored value differs from what the row held. A form
   *  that sent everything would record an override for every field and freeze
   *  the row from later replays — only a real edit is protected. */
  function diff(): OpportunityUpdate {
    const out: OpportunityUpdate = {};
    const pairs: Array<[keyof typeof fields, keyof OpportunityUpdate]> = [
      ["company_name_raw", "company_name_raw"],
      ["job_title_raw", "job_title_raw"],
      ["location_raw", "location_raw"],
      ["salary_raw", "salary_raw"],
      ["working_hours_raw", "working_hours_raw"],
      ["duration_raw", "duration_raw"],
      ["employment_type", "employment_type"],
      ["job_description", "job_description"],
      ["requirements", "requirements"],
    ];
    for (const [formKey, bodyKey] of pairs) {
      const next = orNull(fields[formKey]);
      const before = row[bodyKey as keyof Opportunity] ?? null;
      if (next !== before) out[bodyKey] = next;
    }
    return out;
  }

  async function save() {
    if (saving) return;
    const body = diff();
    // Nothing changed: closing is the honest outcome, and a save that writes
    // nothing but a read-back is a request nobody asked for.
    if (Object.keys(body).length === 0) {
      onCancel();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const saved = (await updateOpportunity(row.id, body)) as Opportunity;
      onSaved(saved);
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not save that just now.");
      setSaving(false);
    }
  }

  const dirty = Object.keys(diff()).length > 0;

  return (
    <form
      className="jo-edit-form"
      aria-label="Edit job order details"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <div className="jo-form-grid jo-edit-grid">
        <Field
          label="Job title"
          value={fields.job_title_raw}
          onChange={set("job_title_raw")}
          placeholder="Warehouse assistant"
        />
        <Field
          label="Company"
          value={fields.company_name_raw}
          onChange={set("company_name_raw")}
          placeholder="Sunrise Logistics Pte Ltd"
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

      {error && (
        <p className="jo-form-error" role="alert">
          {error}
        </p>
      )}

      <div className="jo-edit-actions">
        <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
        <button type="submit" className="btn btn-primary" disabled={!dirty || saving}>
          {saving ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

/** One labelled free-text field — the same field the create dialog uses, so
 *  editing and creating a job order read identically. */
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
