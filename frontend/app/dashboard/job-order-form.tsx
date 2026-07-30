"use client";

import { useEffect, useId, useRef, useState } from "react";

import { CLIENTS_PATH } from "../api";
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

/** Waited out after the last keystroke before the client search reaches the
 *  server. The same value `useOpportunities` uses for its own search box —
 *  live enough to feel immediate, long enough that a five-letter company is
 *  one request rather than five. */
const CLIENT_SEARCH_DEBOUNCE_MS = 300;

/** How many matches the picker offers. A short list is scannable; a recruiter
 *  who cannot see the one they want types another letter, which is cheaper
 *  than scrolling. */
const CLIENT_SEARCH_LIMIT = 8;

/** Only what the picker draws. Deliberately not `Client` from `clients.ts`:
 *  this reads two fields of a list row and typing it as the whole record
 *  would suggest the rest is available here. */
type ClientMatch = { id: string; name: string };

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

/**
 * Type-to-search over the agency's clients.
 *
 * Not `MemberPicker`, and not a `<select>`. The members list is 3-50 people
 * and loads once; clients are paginated and an agency accumulates hundreds of
 * them, so a preloaded dropdown would either be a very long list or a quietly
 * truncated one. The query goes to the server as `?q=`.
 *
 * Leaving it empty is an ordinary outcome, not a failed selection: a company
 * nobody has recorded yet has no row to point at, which is exactly what a
 * nullable `client_id` is for. So typing without choosing a match sends no
 * client at all rather than blocking the save — and the text is not smuggled
 * into the client field either. It goes in `company_name_raw`, where an
 * unrecorded company name belongs.
 */
function ClientPicker({
  value,
  onSelect,
}: {
  value: ClientMatch | null;
  onSelect: (client: ClientMatch | null) => void;
}) {
  const inputId = useId();
  const listId = useId();
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<ClientMatch[]>([]);
  const [open, setOpen] = useState(false);
  // Every search takes a ticket and only the newest may write, the same rule
  // the lists use: "sun" and "sunr" are two requests and the shorter one can
  // land second, putting the wider set of matches under the narrower text.
  const generation = useRef(0);

  useEffect(() => {
    const term = query.trim();
    if (term.length === 0) {
      setMatches([]);
      return;
    }
    const timer = setTimeout(async () => {
      const mine = ++generation.current;
      try {
        const params = new URLSearchParams({ q: term, limit: String(CLIENT_SEARCH_LIMIT) });
        const res = await fetch(`${CLIENTS_PATH}?${params.toString()}`, {
          credentials: "include",
          headers: { Accept: "application/json" },
        });
        if (mine !== generation.current || !res.ok) return;
        const body = (await res.json()) as { items?: ClientMatch[] };
        if (mine !== generation.current) return;
        setMatches(body.items ?? []);
        setOpen(true);
      } catch {
        // Silent: the field is optional, and an error banner over a client
        // lookup would report a problem with a job order that is fine.
      }
    }, CLIENT_SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  return (
    <div className="jo-form-field jo-client-picker">
      <label htmlFor={inputId}>Client</label>
      <input
        id={inputId}
        className="jo-search"
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        autoComplete="off"
        value={value ? value.name : query}
        placeholder="Search, or leave empty"
        onChange={(event) => {
          // Editing after a selection un-selects it. The text no longer names
          // the client that was chosen, and keeping the id would file the job
          // order under a company the recruiter has visibly typed away from.
          if (value) onSelect(null);
          setQuery(event.target.value);
        }}
        // Deferred, like `MemberPicker`'s: a blur fires before the click that
        // caused it, so closing immediately would close the list out from
        // under the match being chosen.
        onBlur={() => window.setTimeout(() => setOpen(false), 0)}
        onFocus={() => matches.length > 0 && setOpen(true)}
      />
      {open && matches.length > 0 && !value && (
        <ul id={listId} className="jo-client-matches" role="listbox">
          {matches.map((match) => (
            <li key={match.id}>
              <button
                type="button"
                role="option"
                aria-selected={false}
                onClick={() => {
                  onSelect(match);
                  setOpen(false);
                }}
              >
                {match.name}
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="jo-form-hint">
        Leave this empty if the company is not on our list yet. The name still goes in Company.
      </p>
    </div>
  );
}

/** One labelled free-text field. A component rather than nine copies of the
 *  same three elements, so the label and the input cannot drift apart. */
function Field({
  label,
  value,
  onChange,
  multiline,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  multiline?: boolean;
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
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          id={id}
          className="jo-search"
          type="text"
          value={value}
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
        <Field label="Job title" value={fields.job_title_raw} onChange={set("job_title_raw")} />
        <Field label="Company" value={fields.company_name_raw} onChange={set("company_name_raw")} />
        <ClientPicker value={client} onSelect={setClient} />
        <Field label="Location" value={fields.location_raw} onChange={set("location_raw")} />
        <Field label="Pay" value={fields.salary_raw} onChange={set("salary_raw")} />
        <Field
          label="Working hours"
          value={fields.working_hours_raw}
          onChange={set("working_hours_raw")}
        />
        <Field label="How long it runs" value={fields.duration_raw} onChange={set("duration_raw")} />
        <Field
          label="Kind of work"
          value={fields.employment_type}
          onChange={set("employment_type")}
        />
      </div>

      <Field
        label="What the job involves"
        value={fields.job_description}
        onChange={set("job_description")}
        multiline
      />
      <Field
        label="What they are looking for"
        value={fields.requirements}
        onChange={set("requirements")}
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
