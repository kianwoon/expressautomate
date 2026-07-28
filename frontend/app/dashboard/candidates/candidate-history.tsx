"use client";

import { useEffect, useRef, useState } from "react";

import type { Candidate, CandidateRole, CandidateRoleBody, DatePrecision } from "../candidates";
import { createCandidateRole, deleteCandidateRole, updateCandidateRole } from "../candidates";

/**
 * The candidate's career, rather than the one line of it the record used to
 * carry.
 *
 * `current_title`, `current_employer` and `years_experience` are all derived
 * from these rows by the server, so this is the only place they can honestly
 * be corrected: editing a role recomputes the candidate, and the panel
 * refetches. Nothing here writes a derived field directly.
 *
 * Two rules shape almost every decision below.
 *
 * The first is §15 — never assert a fact no source stated. A CV that says
 * "2019" carries no month, so the row shows "2019" and the editor never
 * invents one. That is why the dates are a month/year pair of selects rather
 * than `<input type="date">`, which cannot express "I only know the year" at
 * all, and why `formatRoleDate` takes the precision as an argument instead of
 * guessing from the value.
 *
 * The second is the house style set by `candidate-avatar.tsx`: the controls
 * belong on the thing they act on. There is no column of grey buttons beside
 * the timeline. Editing and removing a role are small icon buttons on the
 * role's own row, revealed on hover *and* on focus — they stay in the tab
 * order at all times, because an affordance only a mouse can find is a defect
 * rather than a style.
 */

/** Full names for the select, short ones for the rendered date. Both are the
 *  Gregorian calendar rather than anything configurable, and both are indexed
 *  by month number minus one. */
const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

const MONTH_SHORT = MONTH_NAMES.map((name) => name.slice(0, 3));

/** A stored date, split without going anywhere near `new Date()`. Parsing
 *  "2019-03-01" as a Date reads it as UTC midnight and then renders it in the
 *  browser's zone, which west of Greenwich turns March into February. The
 *  string is already the calendar date we want; nothing is gained by
 *  converting it to an instant and back. */
function parseIso(value: string): { year: number; month: number; day: number } | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return null;
  return { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
}

/**
 * A date rendered at exactly the precision the source recorded — "2019",
 * "Mar 2019", "3 Mar 2019" — and never a digit more.
 *
 * A null precision is the awkward case: the column is nullable, so an import
 * that never recorded one leaves a full date sitting beside no claim about
 * how much of it is real. Falling back to the year is the only reading that
 * cannot assert something false. It loses detail; the alternative invents it.
 */
export function formatRoleDate(value: string | null, precision: DatePrecision | null): string | null {
  if (!value) return null;
  const parts = parseIso(value);
  if (!parts) return null;
  if (precision === "day") return `${parts.day} ${MONTH_SHORT[parts.month - 1]} ${parts.year}`;
  if (precision === "month") return `${MONTH_SHORT[parts.month - 1]} ${parts.year}`;
  return String(parts.year);
}

/** A year-only date says nothing about the month, so counting from January
 *  would overstate the role by up to a year and December would understate it.
 *  July splits the difference. The same constant, for the same reason, as
 *  `_YEAR_ONLY_MONTH` in `backend/app/services/candidate_tenure.py` — the
 *  duration on this row has to agree with the years the server derived from
 *  the very same rows, and two different roundings would put a visible
 *  contradiction on one screen. */
const YEAR_ONLY_MONTH = 7;

/** Months since year zero, so a difference is a subtraction. Pinned to the
 *  month, matching the server's half-open interval: a role that started and
 *  ended inside one month covers zero whole months, not one. */
function pinnedMonths(value: string, precision: DatePrecision | null): number | null {
  const parts = parseIso(value);
  if (!parts) return null;
  const month = precision === "year" ? YEAR_ONLY_MONTH : parts.month;
  return parts.year * 12 + (month - 1);
}

function durationMonths(role: CandidateRole, now: Date): number | null {
  if (!role.started_on) return null;
  const start = pinnedMonths(role.started_on, role.started_precision);
  if (start == null) return null;
  const end = role.ended_on
    ? pinnedMonths(role.ended_on, role.ended_precision)
    : now.getFullYear() * 12 + now.getMonth();
  if (end == null) return null;
  return Math.max(0, end - start);
}

function formatDuration(months: number): string {
  const years = Math.floor(months / 12);
  const rest = months % 12;
  const parts: string[] = [];
  if (years > 0) parts.push(`${years} yr${years === 1 ? "" : "s"}`);
  if (rest > 0 || years === 0) parts.push(`${rest} mo${rest === 1 ? "" : "s"}`);
  return parts.join(" ");
}

/** "Mar 2019 – Present", or "2016 – 2019", or the honest shrug. The dash is an
 *  en dash with spaces around it, which is what a range takes. */
function rangeLabel(role: CandidateRole): string {
  const from = formatRoleDate(role.started_on, role.started_precision);
  const to = role.ended_on ? formatRoleDate(role.ended_on, role.ended_precision) : "Present";
  if (!from) return to === "Present" ? "Dates not mentioned" : `Until ${to}`;
  return `${from} – ${to}`;
}

/* ------------------------------------------------------------------ editor */

type Draft = {
  employer: string;
  title: string;
  started_on: string | null;
  started_precision: DatePrecision | null;
  ended_on: string | null;
  ended_precision: DatePrecision | null;
  employment_type: string | null;
  location: string | null;
  description: string | null;
};

/** What the two selects hold. `month` is deliberately allowed to be empty
 *  while `year` is set: that pair is precisely how the editor expresses "the
 *  source only gave me a year", and it is the reason a date input could not
 *  be used here. */
type YearMonth = { year: string; month: string };

function toYearMonth(value: string | null): YearMonth {
  if (!value) return { year: "", month: "" };
  const parts = parseIso(value);
  if (!parts) return { year: "", month: "" };
  return { year: String(parts.year), month: String(parts.month) };
}

/**
 * The pair of selects turned back into a date and a precision.
 *
 * `original` is the value the row already held. If the recruiter never
 * touched the year or the month, the stored date is handed back untouched —
 * which is what stops a day-precision role imported from somewhere else from
 * being quietly rounded down to its month the first time somebody edits the
 * location beside it. The editor cannot *create* day precision; it must not
 * destroy it either.
 */
function fromYearMonth(
  picked: YearMonth,
  original: { on: string | null; precision: DatePrecision | null },
): { on: string | null; precision: DatePrecision | null } {
  if (!picked.year) return { on: null, precision: null };
  const before = toYearMonth(original.on);
  if (original.on && before.year === picked.year && before.month === picked.month) {
    return { on: original.on, precision: original.precision };
  }
  const month = picked.month ? picked.month.padStart(2, "0") : "01";
  return {
    on: `${picked.year}-${month}-01`,
    precision: picked.month ? "month" : "year",
  };
}

function orNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/** Only the keys whose value actually differs. The PATCH endpoint is a true
 *  partial update — an omitted key is left alone and an explicit `null`
 *  clears the field — so sending the whole draft every time would turn a
 *  one-word title fix into a rewrite of nine columns, including any the
 *  parser will one day fill in. */
function changedKeys(before: CandidateRole, after: Draft): CandidateRoleBody {
  const body: CandidateRoleBody = {};
  if (after.employer !== before.employer) body.employer = after.employer;
  if (after.title !== before.title) body.title = after.title;
  if (after.started_on !== before.started_on) body.started_on = after.started_on;
  if (after.started_precision !== before.started_precision) {
    body.started_precision = after.started_precision;
  }
  if (after.ended_on !== before.ended_on) body.ended_on = after.ended_on;
  if (after.ended_precision !== before.ended_precision) {
    body.ended_precision = after.ended_precision;
  }
  if (after.employment_type !== before.employment_type) {
    body.employment_type = after.employment_type;
  }
  if (after.location !== before.location) body.location = after.location;
  if (after.description !== before.description) body.description = after.description;
  return body;
}

/** Wide enough for a career and one year ahead for a role already agreed but
 *  not yet started. Computed from today rather than written down, so the list
 *  does not quietly go stale in a year's time. */
function yearOptions(): number[] {
  const thisYear = new Date().getFullYear();
  const years: number[] = [];
  for (let y = thisYear + 1; y >= thisYear - 60; y -= 1) years.push(y);
  return years;
}

function DatePicker({
  legend,
  value,
  onChange,
  disabled,
}: {
  legend: string;
  value: YearMonth;
  onChange: (next: YearMonth) => void;
  disabled?: boolean;
}) {
  return (
    <fieldset className="ch-dates" disabled={disabled}>
      <legend className="row-k">{legend}</legend>
      <select
        className="gl-input ch-select"
        value={value.month}
        aria-label={`${legend} month`}
        onChange={(event) => onChange({ ...value, month: event.target.value })}
      >
        {/* "Month unknown" is a real answer, not a placeholder to be filled
            in. Choosing it is what records year precision. */}
        <option value="">Month unknown</option>
        {MONTH_NAMES.map((name, index) => (
          <option key={name} value={String(index + 1)}>
            {name}
          </option>
        ))}
      </select>
      <select
        className="gl-input ch-select"
        value={value.year}
        aria-label={`${legend} year`}
        onChange={(event) => {
          const year = event.target.value;
          // No year means no date at all, and a month hanging off nothing is
          // not something the record can hold.
          onChange(year ? { ...value, year } : { year: "", month: "" });
        }}
      >
        <option value="">Not mentioned</option>
        {yearOptions().map((year) => (
          <option key={year} value={String(year)}>
            {year}
          </option>
        ))}
      </select>
    </fieldset>
  );
}

function RoleEditor({
  role,
  busy,
  onSave,
  onCancel,
}: {
  /** `null` when adding. The editor is otherwise identical either way — the
   *  only difference is whether the result is POSTed or diffed and PATCHed,
   *  and that belongs to the caller. */
  role: CandidateRole | null;
  busy: boolean;
  onSave: (draft: Draft) => void;
  onCancel: () => void;
}) {
  const [employer, setEmployer] = useState(role?.employer ?? "");
  const [title, setTitle] = useState(role?.title ?? "");
  const [start, setStart] = useState<YearMonth>(toYearMonth(role?.started_on ?? null));
  const [end, setEnd] = useState<YearMonth>(toYearMonth(role?.ended_on ?? null));
  const [current, setCurrent] = useState(role ? role.ended_on === null : true);
  const [employmentType, setEmploymentType] = useState(role?.employment_type ?? "");
  const [location, setLocation] = useState(role?.location ?? "");
  const [description, setDescription] = useState(role?.description ?? "");
  const firstRef = useRef<HTMLInputElement | null>(null);

  // The editor replaces the row it belongs to, so the row's own controls have
  // just left the document. Without this, focus falls to the body and the
  // next Tab starts again at the top of the page.
  useEffect(() => {
    firstRef.current?.focus();
  }, []);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const started = fromYearMonth(start, {
      on: role?.started_on ?? null,
      precision: role?.started_precision ?? null,
    });
    // "Still here" is the truth being recorded; the end date is merely how it
    // is stored. Ticking the box clears both halves, so a leftover year from
    // before it was ticked cannot survive as a contradiction.
    const ended = current
      ? { on: null, precision: null }
      : fromYearMonth(end, {
          on: role?.ended_on ?? null,
          precision: role?.ended_precision ?? null,
        });
    onSave({
      employer: employer.trim(),
      title: title.trim(),
      started_on: started.on,
      started_precision: started.precision,
      ended_on: ended.on,
      ended_precision: ended.precision,
      employment_type: orNull(employmentType),
      location: orNull(location),
      description: orNull(description),
    });
  }

  const ready = employer.trim() !== "" && title.trim() !== "";

  return (
    <form className="ch-editor" onSubmit={submit} aria-label={role ? "Edit this role" : "Add a role"}>
      <div className="gl-form-grid">
        <label className="gl-field">
          <span className="row-k">Employer</span>
          <input
            ref={firstRef}
            className="gl-input"
            value={employer}
            onChange={(event) => setEmployer(event.target.value)}
            required
          />
        </label>
        <label className="gl-field">
          <span className="row-k">Title</span>
          <input
            className="gl-input"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            required
          />
        </label>

        <div className="gl-field">
          <DatePicker legend="Started" value={start} onChange={setStart} />
        </div>
        <div className="gl-field">
          <DatePicker legend="Ended" value={end} onChange={setEnd} disabled={current} />
          <label className="ch-current">
            <input
              type="checkbox"
              checked={current}
              onChange={(event) => setCurrent(event.target.checked)}
            />
            <span>Still in this role</span>
          </label>
        </div>

        <label className="gl-field">
          <span className="row-k">Employment type</span>
          <input
            className="gl-input"
            value={employmentType}
            onChange={(event) => setEmploymentType(event.target.value)}
            placeholder="Permanent, contract…"
          />
        </label>
        <label className="gl-field">
          <span className="row-k">Location</span>
          <input
            className="gl-input"
            value={location}
            onChange={(event) => setLocation(event.target.value)}
          />
        </label>
        <label className="gl-field gl-field-wide">
          <span className="row-k">What they did</span>
          <textarea
            className="gl-input"
            rows={3}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
      </div>

      <div className="gl-form-actions">
        <button type="submit" className="btn btn-primary" disabled={busy || !ready}>
          {busy ? "Saving…" : "Save role"}
        </button>
        <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/* ---------------------------------------------------------------- timeline */

function RoleItem({
  role,
  now,
  busy,
  focused,
  onEdit,
  onRemove,
}: {
  role: CandidateRole;
  now: Date;
  busy: boolean;
  /** True for the role whose editor just closed. Focus has to be handed back
   *  to the control that opened it, or a keyboard lands at the top of the
   *  document every time a role is saved. */
  focused: boolean;
  onEdit: () => void;
  onRemove: () => void;
}) {
  const editRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (focused) editRef.current?.focus();
  }, [focused]);

  const months = durationMonths(role, now);
  const meta = [role.employment_type, role.location].filter(Boolean).join(" · ");

  return (
    <li className="ch-item" data-current={role.is_current ? "yes" : undefined} data-status={role.status}>
      <div className="ch-line">
        <div className="ch-who">
          <span className="ch-title">{role.title}</span>
          <span className="ch-at"> at </span>
          <span className="ch-employer">{role.employer}</span>
        </div>
        {/* On the row, revealed by hover or by focus, never removed from the
            tab order — the same bargain the avatar's camera overlay strikes. */}
        <div className="ch-tools">
          <button
            ref={editRef}
            type="button"
            className="ch-tool"
            onClick={onEdit}
            disabled={busy}
            title="Edit this role"
            aria-label={`Edit ${role.title} at ${role.employer}`}
          >
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M4 20h4l10-10-4-4L4 16z" strokeLinejoin="round" />
            </svg>
          </button>
          <button
            type="button"
            className="ch-tool ch-tool-remove"
            onClick={onRemove}
            disabled={busy}
            title="Remove this role"
            aria-label={`Remove ${role.title} at ${role.employer}`}
          >
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </div>

      <p className="ch-when">
        {rangeLabel(role)}
        {months != null && <span className="ch-dur"> · {formatDuration(months)}</span>}
      </p>
      {meta && <p className="ch-meta">{meta}</p>}

      {role.description && (
        // A native disclosure: it is a summary and a region, keyboard-operable
        // and announced as expanded or collapsed, with nothing for us to
        // reimplement badly.
        <details className="ch-more">
          <summary>What they did</summary>
          <p className="body ch-desc">{role.description}</p>
        </details>
      )}

      {role.status === "unconfirmed" && (
        // Nothing writes this status yet — a recruiter typing a role gets
        // `confirmed`. It renders now so that the CV parser which will write
        // `unconfirmed` rows is an endpoint rather than a redesign, and the
        // buttons are disabled rather than absent so the shape of that future
        // is visible and honest about not working today.
        <div className="ch-verify">
          <span className="ch-flag">From a CV, not yet checked</span>
          <button type="button" className="ch-tool" disabled title="Confirming arrives with CV parsing">
            Confirm
          </button>
          <button type="button" className="ch-tool" disabled title="Rejecting arrives with CV parsing">
            Reject
          </button>
        </div>
      )}
    </li>
  );
}

export function CandidateHistory({
  row,
  onChanged,
}: {
  row: Candidate;
  /** Refetch this candidate only. A role change moves `years_experience`,
   *  `current_title` and `current_employer` on the record, all of which the
   *  panel above is showing — but the list does not draw any of them per row,
   *  so reloading it would blank the table for nothing. */
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refocus, setRefocus] = useState<string | null>(null);
  const addRef = useRef<HTMLButtonElement | null>(null);

  // Today, read once per render rather than per role, so a list of durations
  // is computed against one moment instead of a dozen slightly different ones.
  const now = new Date();
  const roles = row.roles;

  async function run(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (err) {
      // `readError` has already turned the 422 into the server's own sentence
      // — "A role cannot end before it starts" tells the recruiter which end
      // to fix, which no message written here could.
      setError(err instanceof Error ? err.message : "We could not save that just now.");
    } finally {
      setBusy(false);
    }
  }

  function save(roleId: string | null, draft: Draft) {
    void run(async () => {
      if (roleId) {
        const before = (roles ?? []).find((r) => r.id === roleId);
        // No `before` would mean the row vanished under the open editor; a
        // full body is then the only honest thing to send.
        const body = before ? changedKeys(before, draft) : draft;
        if (Object.keys(body).length > 0) await updateCandidateRole(row.id, roleId, body);
      } else {
        await createCandidateRole(row.id, draft);
      }
      setEditing(null);
      setAdding(false);
      setRefocus(roleId);
      if (!roleId) addRef.current?.focus();
      onChanged();
    });
  }

  function remove(role: CandidateRole) {
    if (!window.confirm(`Remove ${role.title} at ${role.employer}? This cannot be undone.`)) return;
    void run(async () => {
      await deleteCandidateRole(row.id, role.id);
      // The button that was pressed is gone with the row. Hand focus to the
      // one control on this section that is always there.
      addRef.current?.focus();
      onChanged();
    });
  }

  return (
    <section className="ch-section" aria-label="Work history">
      <div className="ch-head">
        <span className="row-k">Work history</span>
        {/* One quiet control for the section, set as a link rather than a
            button block: it acts on the list as a whole, so it has nowhere
            else to live, but it is not the thing on this panel worth the
            most weight. */}
        <button
          ref={addRef}
          type="button"
          className="ch-add"
          onClick={() => {
            setAdding(true);
            setEditing(null);
          }}
          disabled={busy || adding}
        >
          + Add a role
        </button>
      </div>

      {roles === undefined ? (
        // Absent is not empty. The list endpoint does not send roles, so a row
        // that arrived from the table has simply not been asked yet, and
        // "No roles recorded" would be a claim about the candidate rather than
        // about what has loaded.
        <p className="body muted ch-empty">Loading their history…</p>
      ) : roles.length === 0 && !adding ? (
        <p className="body muted ch-empty">
          No roles recorded yet. Their experience is derived from these, so adding one fills in the
          title, employer and years above.
        </p>
      ) : (
        <ol className="ch-list">
          {roles.map((role) =>
            editing === role.id ? (
              <li className="ch-item ch-item-editing" key={role.id}>
                <RoleEditor
                  role={role}
                  busy={busy}
                  onSave={(draft) => save(role.id, draft)}
                  onCancel={() => {
                    setEditing(null);
                    setRefocus(role.id);
                  }}
                />
              </li>
            ) : (
              <RoleItem
                key={role.id}
                role={role}
                now={now}
                busy={busy}
                focused={refocus === role.id}
                onEdit={() => {
                  setRefocus(null);
                  setEditing(role.id);
                  setAdding(false);
                }}
                onRemove={() => remove(role)}
              />
            ),
          )}
        </ol>
      )}

      {adding && (
        <div className="ch-item ch-item-editing">
          <RoleEditor
            role={null}
            busy={busy}
            onSave={(draft) => save(null, draft)}
            onCancel={() => {
              setAdding(false);
              addRef.current?.focus();
            }}
          />
        </div>
      )}

      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
