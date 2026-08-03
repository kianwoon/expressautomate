"use client";

import { useId, useState } from "react";

import { ClientSearch, type ClientMatch } from "./client-search";
import { Dialog } from "./dialog";
import { createOpportunity, type ManualOpportunity } from "./opportunity-actions";

/**
 * A job order that never arrived as an email.
 *
 * Some vacancies are taken over the phone or on WhatsApp while the client is
 * still talking, and until this form existed the only way one entered the
 * system was through a mailbox. That made every hand-taken job order invisible
 * to the very list that is supposed to be all of them.
 *
 * Every field is free text and every one is optional but the title, because
 * the recruiter is transcribing what they were told. Turning "6k neg." into a
 * number here would be the fabrication §15 forbids, and normalising it is the
 * extraction pipeline's job — which never runs on these rows.
 *
 * There is no assignee control, and its absence is deliberate: the server
 * assigns the row to whoever created it. You typed it in, so it is yours,
 * not the client account holder's.
 *
 * allow-hardcode: the strings below are user-facing labels and copy rendered
 * to the page, not a list anything is matched against.
 */

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

  const set = (key: keyof typeof fields) => (next: string) =>
    setFields((current) => ({ ...current, [key]: next }));

  // The one thing required. A row with no title is not a job order anybody
  // could work from, and it would sit in the list as a blank line.
  const ready = fields.job_title_raw.trim().length > 0;

  async function save() {
    if (!ready || saving) return;
    setSaving(true);
    setError(null);
    const body: ManualOpportunity = {
      client_id: client ? client.id : null,
      job_title_raw: orNull(fields.job_title_raw),
      company_name_raw: orNull(fields.company_name_raw),
      location_raw: orNull(fields.location_raw),
      salary_raw: orNull(fields.salary_raw),
      working_hours_raw: orNull(fields.working_hours_raw),
      duration_raw: orNull(fields.duration_raw),
      employment_type: orNull(fields.employment_type),
      job_description: orNull(fields.job_description),
      requirements: orNull(fields.requirements),
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
        For a vacancy taken over the phone or on WhatsApp. Type what you were told — anything you
        were not told, leave empty.
      </p>

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
