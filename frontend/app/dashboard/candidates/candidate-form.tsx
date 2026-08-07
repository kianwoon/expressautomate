"use client";

import { useState, type FormEvent, type ReactNode } from "react";

import {
  ApiError,
  CandidateCollisionError,
  createCandidate,
  requestCandidateAccess,
  updateCandidate,
  type Candidate,
  type CandidateCollision,
  type Stage,
} from "../candidates";
import { Dialog } from "../dialog";

/**
 * Create and edit, in one form.
 *
 * Only `full_name` is required — a recruiter frequently has a name and a
 * phone number and nothing else (`CandidateIn` on the backend agrees). The
 * submit button stays disabled while it is blank rather than letting the
 * request go and fail 422.
 *
 * A 409 renders inline, verbatim, above the form: it is a split-identity
 * conflict or a key someone else already holds, and the server's own message
 * names the other candidate. Replacing it with a generic failure would make
 * the feature unusable at exactly the moment it matters.
 *
 * allow-hardcode: the field labels and placeholders below are user-facing
 * form copy rendered to the page, not a list anything is matched against.
 */

/** The blank option is the point of these lists, not an artefact of building a
 *  `<select>`. "Not recorded" has to stay reachable *after* a value has been
 *  chosen: a recruiter who picks the wrong one, or who learns they were told
 *  wrong, must be able to get back to knowing nothing. Without it the only way
 *  out of a mistake is another guess, and a guess entered here is what the
 *  Work Permit check then treats as fact. */
const NOT_RECORDED = "";

export const SEXES: { value: string; label: string }[] = [
  { value: NOT_RECORDED, label: "Not recorded" },
  { value: "female", label: "Female" },
  { value: "male", label: "Male" },
];

export const RACES: { value: string; label: string }[] = [
  { value: NOT_RECORDED, label: "Not recorded" },
  { value: "chinese", label: "Chinese" },
  { value: "malay", label: "Malay" },
  { value: "indian", label: "Indian" },
  { value: "others", label: "Others" },
];

/** A `<datalist>`, not a `<select>`: the backend takes any ISO 3166-1 alpha-2
 *  code, so the field must stay open, but a recruiter typing "Philippines"
 *  into a bare text box earns a 422 and no idea why. These are the MDW
 *  approved source countries plus Singapore, which is where the codes a
 *  recruiter here actually types come from — a hint at the common cases, not
 *  a restriction, and not a list anything is matched against.
 *
 *  allow-hardcode: user-facing datalist suggestions rendered to the page. The
 *  rule these mirror lives in `MDW_APPROVED_SOURCE_COUNTRIES` on the server,
 *  which is what eligibility is actually evaluated against; nothing here is
 *  read back or compared. */
export const NATIONALITY_HINTS: { code: string; name: string }[] = [
  { code: "SG", name: "Singapore" },
  { code: "PH", name: "Philippines" },
  { code: "ID", name: "Indonesia" },
  { code: "MM", name: "Myanmar" },
  { code: "IN", name: "India" },
  { code: "LK", name: "Sri Lanka" },
  { code: "TH", name: "Thailand" },
  { code: "MY", name: "Malaysia" },
  { code: "KH", name: "Cambodia" },
  { code: "BD", name: "Bangladesh" },
  { code: "HK", name: "Hong Kong" },
  { code: "MO", name: "Macau" },
  { code: "TW", name: "Taiwan" },
  { code: "KR", name: "South Korea" },
];

export const STAGES: { value: Stage; label: string }[] = [
  { value: "new", label: "New" },
  { value: "contacted", label: "Contacted" },
  { value: "submitted", label: "Submitted" },
  { value: "placed", label: "Placed" },
  { value: "rejected", label: "Rejected" },
];

export type FormState = {
  full_name: string;
  email: string;
  phone_raw: string;
  current_title: string;
  current_employer: string;
  location: string;
  years_experience: string;
  expected_salary: string;
  salary_currency: string;
  salary_period: string;
  available_from: string;
  notice_period_raw: string;
  employment_type: string;
  notes: string;
  pipeline_stage: Stage;
  skills: string;
  sex: string;
  race: string;
  race_detail: string;
  nationality: string;
  date_of_birth: string;
  education_years: string;
};

export function toFormState(row: Candidate | null): FormState {
  return {
    full_name: row?.full_name ?? "",
    email: row?.email ?? "",
    phone_raw: row?.phone_raw ?? "",
    current_title: row?.current_title ?? "",
    current_employer: row?.current_employer ?? "",
    location: row?.location ?? "",
    years_experience: row?.years_experience != null ? String(row.years_experience) : "",
    expected_salary: row?.expected_salary != null ? String(row.expected_salary) : "",
    salary_currency: row?.salary_currency ?? "",
    salary_period: row?.salary_period ?? "",
    available_from: row?.available_from ?? "",
    notice_period_raw: row?.notice_period_raw ?? "",
    employment_type: row?.employment_type ?? "",
    notes: row?.notes ?? "",
    pipeline_stage: row?.pipeline_stage ?? "new",
    skills: row?.skills ? row.skills.join(", ") : "",
    sex: row?.sex ?? "",
    race: row?.race ?? "",
    race_detail: row?.race_detail ?? "",
    nationality: row?.nationality ?? "",
    date_of_birth: row?.date_of_birth ?? "",
    education_years: row?.education_years != null ? String(row.education_years) : "",
  };
}

/** Blank strings become `null`, not `""` — an empty string is a value the
 *  email or the recruiter stated; the backend's `Not mentioned` convention
 *  means absent fields must actually be absent. */
function orNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

type SubmitBody = ReturnType<typeof toSubmitBody>;

export function toSubmitBody(form: FormState) {
  return {
    full_name: form.full_name.trim(),
    email: orNull(form.email),
    phone_raw: orNull(form.phone_raw),
    current_title: orNull(form.current_title),
    current_employer: orNull(form.current_employer),
    location: orNull(form.location),
    years_experience: form.years_experience.trim() === "" ? null : Number(form.years_experience),
    expected_salary: form.expected_salary.trim() === "" ? null : Number(form.expected_salary),
    salary_currency: orNull(form.salary_currency),
    salary_period: orNull(form.salary_period),
    available_from: orNull(form.available_from),
    notice_period_raw: orNull(form.notice_period_raw),
    employment_type: orNull(form.employment_type),
    notes: orNull(form.notes),
    pipeline_stage: form.pipeline_stage,
    skills: form.skills
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    // `orNull` throughout, so clearing a field back to blank sends an explicit
    // `null` and the record returns to "not recorded". A form that could only
    // ever set these would make the first guess permanent.
    // Cast rather than parse: the only values these can hold come from the
    // `<option>` lists above, which are the same literals the API accepts, and
    // the server rejects anything else regardless.
    sex: orNull(form.sex) as Candidate["sex"],
    race: orNull(form.race) as Candidate["race"],
    // Tied to `race`, because a detail left behind after the race it described
    // was changed is a stale claim about a person, and the pair would disagree
    // with each other on the record.
    race_detail: form.race === "others" ? orNull(form.race_detail) : null,
    nationality: orNull(form.nationality),
    date_of_birth: orNull(form.date_of_birth),
    education_years: form.education_years.trim() === "" ? null : Number(form.education_years),
  };
}

/** Only the keys whose value actually differs from the row this form was
 *  populated from. `full_name` and `pipeline_stage` always ride along —
 *  the former is required by the backend on every PATCH shape it accepts
 *  in this form, the latter is not override-protected — but every other
 *  field is sent only when the recruiter changed it. */
export function changedFields(next: SubmitBody, initialForm: FormState): Partial<SubmitBody> {
  const before = toSubmitBody(initialForm);
  const out: Partial<SubmitBody> = {
    full_name: next.full_name,
    pipeline_stage: next.pipeline_stage,
  };
  (Object.keys(next) as (keyof SubmitBody)[]).forEach((key) => {
    if (key === "skills") {
      if (JSON.stringify(next.skills) !== JSON.stringify(before.skills)) out.skills = next.skills;
      return;
    }
    if (next[key] !== before[key]) {
      (out as Record<string, unknown>)[key] = next[key];
    }
  });
  return out;
}

export function CandidateForm({
  row,
  onDone,
  onCancel,
}: {
  /** `null` for create. */
  row: Candidate | null;
  onDone: (saved: Candidate) => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<FormState>(() => toFormState(row));
  // Captured once, at mount, so an edit can be diffed against what the row
  // actually held — not against whatever the form happens to hold by the
  // time the recruiter submits.
  const [initial] = useState<FormState>(() => toFormState(row));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Kept apart from `error`, and that separation is the whole point. A
  // collision is not a failure the recruiter caused and not a field they can
  // fix: the person exists, a colleague holds them, and the only next move is
  // to ask. Folded into `error` it would render as red text above a form that
  // still looks like it wants correcting.
  const [collision, setCollision] = useState<CandidateCollision | null>(null);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  const canSubmit = form.full_name.trim() !== "" && !saving;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setSaving(true);
    setError(null);
    setCollision(null);

    const fullBody = toSubmitBody(form);

    try {
      // An edit sends only what the recruiter actually changed. Sending the
      // full body would mark every field an override — protecting a typo fix
      // by freezing the other thirteen fields from every later import.
      const body = row ? changedFields(fullBody, initial) : fullBody;
      const saved = row ? await updateCandidate(row.id, body) : await createCandidate(fullBody);
      onDone(saved);
    } catch (err) {
      if (err instanceof CandidateCollisionError) {
        setCollision(err.collision);
      } else {
        setError(err instanceof ApiError ? err.message : "We could not save that just now.");
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog
      titleId="cand-form-title"
      className="dlg-modal-wide"
      onClose={saving ? () => {} : onCancel}
      title={row ? `Edit ${row.full_name}` : "Add candidate"}
    >
      {error && (
        <p className="body jo-detail-error" role="alert" style={{ marginTop: 12 }}>
          {error}
        </p>
      )}

      {collision && <HeldByColleague collision={collision} />}

      <form onSubmit={submit} className="cand-form" style={{ marginTop: 16 }}>
        <Field label="Full name" full required>
          <input
            className="jo-search"
            value={form.full_name}
            onChange={(e) => set("full_name", e.target.value)}
            placeholder="Tan Wei Ming"
            required
            autoFocus
          />
        </Field>
        <Field label="Email">
          <input
            className="jo-search"
            type="email"
            value={form.email}
            onChange={(e) => set("email", e.target.value)}
            placeholder="weiming.tan@gmail.com"
          />
        </Field>
        <Field label="Phone">
          <input
            className="jo-search"
            value={form.phone_raw}
            onChange={(e) => set("phone_raw", e.target.value)}
            placeholder="+65 9123 4567"
          />
        </Field>
        <Field label="Current title">
          <input
            className="jo-search"
            value={form.current_title}
            onChange={(e) => set("current_title", e.target.value)}
            placeholder="Warehouse assistant"
          />
        </Field>
        <Field label="Current employer">
          <input
            className="jo-search"
            value={form.current_employer}
            onChange={(e) => set("current_employer", e.target.value)}
            placeholder="Acme Logistics Pte Ltd"
          />
        </Field>
        <Field label="Location">
          <input
            className="jo-search"
            value={form.location}
            onChange={(e) => set("location", e.target.value)}
            placeholder="Tuas"
          />
        </Field>
        <Field label="Years of experience">
          <input
            className="jo-search"
            type="number"
            value={form.years_experience}
            onChange={(e) => set("years_experience", e.target.value)}
            placeholder="3"
          />
        </Field>
        <Field label="Expected salary">
          <input
            className="jo-search"
            type="number"
            value={form.expected_salary}
            onChange={(e) => set("expected_salary", e.target.value)}
            placeholder="2800"
          />
        </Field>
        <Field label="Salary currency">
          <input
            className="jo-search"
            value={form.salary_currency}
            onChange={(e) => set("salary_currency", e.target.value)}
            placeholder="SGD"
          />
        </Field>
        <Field label="Salary period">
          <input
            className="jo-search"
            value={form.salary_period}
            onChange={(e) => set("salary_period", e.target.value)}
            placeholder="month"
          />
        </Field>
        <Field label="Available from">
          <input
            className="jo-search"
            type="date"
            value={form.available_from}
            onChange={(e) => set("available_from", e.target.value)}
          />
        </Field>
        <Field label="Notice period">
          <input
            className="jo-search"
            value={form.notice_period_raw}
            onChange={(e) => set("notice_period_raw", e.target.value)}
            placeholder="2 weeks"
          />
        </Field>
        <Field label="Employment type">
          <input
            className="jo-search"
            value={form.employment_type}
            onChange={(e) => set("employment_type", e.target.value)}
            placeholder="Full-time"
          />
        </Field>
        <Field label="Stage">
          <select
            className="jo-search"
            value={form.pipeline_stage}
            onChange={(e) => set("pipeline_stage", e.target.value as Stage)}
          >
            {STAGES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Skills (comma separated)" full>
          <input
            className="jo-search"
            value={form.skills}
            onChange={(e) => set("skills", e.target.value)}
            placeholder="forklift operation, inventory management, WMS"
          />
        </Field>
        {/* Two groups, kept apart on purpose. The four above the divider are
            what a Work Permit is decided on; race below it decides nothing.
            Putting them in one block would imply race is another eligibility
            input, which is exactly the reading `redact.py` exists to prevent. */}
        <fieldset className="cand-group cand-group-permit">
          <legend className="row-k">Work Permit details</legend>
          <p className="body jo-sub cand-group-note">
            A Work Permit is granted on these four facts, so the eligibility filter on the
            candidates list can only use what is recorded here. Leave anything you have not been
            told as <em>Not recorded</em> — an unknown is treated as unknown, and keeps someone in
            the list rather than ruling them out.
          </p>
          <Field label="Sex">
            <select
              className="jo-search"
              value={form.sex}
              onChange={(e) => set("sex", e.target.value)}
            >
              {SEXES.map((s) => (
                <option key={s.label} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Date of birth">
            <input
              className="jo-search"
              type="date"
              value={form.date_of_birth}
              onChange={(e) => set("date_of_birth", e.target.value)}
            />
          </Field>
          <Field label="Years of formal education">
            {/* No `min`/`max` here: the range is a configured rule on the
                server, and a copy in this file is a second place for it to be
                wrong. The server names the field and the bound when it
                refuses, and that message renders above the form. */}
            <input
              className="jo-search"
              type="number"
              value={form.education_years}
              onChange={(e) => set("education_years", e.target.value)}
              placeholder="10"
            />
          </Field>
          <Field label="Nationality">
            <input
              className="jo-search"
              list="cand-nationality-hints"
              value={form.nationality}
              onChange={(e) => set("nationality", e.target.value)}
              placeholder="Two-letter country code, e.g. PH"
            />
            <datalist id="cand-nationality-hints">
              {NATIONALITY_HINTS.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.name}
                </option>
              ))}
            </datalist>
          </Field>
        </fieldset>

        <fieldset className="cand-group cand-group-record">
          {/* Not "Race": a legend repeating the field label below it gives the
              control an accessible name of "Race Race", and a screen reader
              user hears the ambiguity a sighted user does not. */}
          <legend className="row-k">For your records only</legend>
          <p className="body jo-sub cand-group-note">
            Kept for your own records only. It is never used to rank, match or shortlist anyone, it
            is never sent to the AI that explains why a candidate fits, and it has no part in the
            Work Permit check above.
          </p>
          <Field label="Race">
            <select
              className="jo-search"
              value={form.race}
              onChange={(e) => set("race", e.target.value)}
            >
              {RACES.map((r) => (
                <option key={r.label} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </Field>
          {form.race === "others" && (
            <Field label="Race detail">
              <input
                className="jo-search"
                value={form.race_detail}
                onChange={(e) => set("race_detail", e.target.value)}
                placeholder="Eurasian"
              />
            </Field>
          )}
        </fieldset>

        <Field label="Notes" full>
          <textarea
            className="jo-search"
            style={{ minHeight: 80 }}
            value={form.notes}
            onChange={(e) => set("notes", e.target.value)}
            placeholder="Available for immediate start; prefers day shift"
          />
        </Field>

        <div className="cand-form-full" style={{ display: "flex", gap: 10, marginTop: 8 }}>
          <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
            {saving ? "Saving…" : row ? "Save changes" : "Add candidate"}
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={saving}>
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

/**
 * "Already registered by Sarah Lim", and a way to ask her.
 *
 * A `role="status"` region, not `role="alert"`, and not a field error. Nothing
 * the recruiter typed is wrong — they met a real person and typed that
 * person's real email. What happened is that a colleague recorded them first
 * and the record is invisible from here, which is a fact about the agency
 * rather than about this form. Marking the email input `aria-invalid` would
 * tell them to change a value that is correct, and a screen reader would
 * announce a validation failure that does not exist.
 *
 * The name is rendered exactly as the server gave it. `abbreviate` in
 * `candidate_matching.py` masks it deliberately — "Wei Ming T." is the whole
 * disclosure, and expanding or prettifying it here would undo the masking in
 * the one place it was designed to hold.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */
export function HeldByColleague({ collision }: { collision: CandidateCollision }) {
  const [state, setState] = useState<"idle" | "asking" | "asked" | "failed">("idle");
  const { full_name, held_by, id } = collision.candidate;

  // The server does NOT send the candidate id with this 409 — see
  // `CandidateCollision.candidate.id`. The button is therefore rendered and
  // disabled with its reason showing, never hidden: a control that vanishes
  // teaches the reader neither that asking is possible nor why it is not
  // available, and "walk to Sarah's desk" is a worse answer when the reader
  // does not even know the feature exists.
  const askable = collision.can_request_access && typeof id === "string" && id !== "";

  async function ask() {
    if (!askable || state === "asking" || state === "asked") return;
    setState("asking");
    try {
      await requestCandidateAccess(id as string, null);
      setState("asked");
    } catch {
      setState("failed");
    }
  }

  return (
    <div className="card cand-held" role="status" style={{ marginTop: 12 }}>
      <p className="body">
        <strong>{full_name}</strong> is already registered by {held_by}.
      </p>
      <p className="body jo-sub">
        Nothing you entered is wrong — this person is already in your agency’s database, and the
        record belongs to a colleague. Ask them to share it rather than creating a second copy.
      </p>
      <div style={{ display: "flex", gap: 10, marginTop: 8, alignItems: "center" }}>
        <button
          type="button"
          className="btn btn-primary"
          onClick={ask}
          disabled={!askable || state === "asking" || state === "asked"}
        >
          {state === "asking" ? "Asking…" : state === "asked" ? "Access requested" : "Request access"}
        </button>
      </div>
      {!askable && collision.can_request_access && (
        <p className="body jo-sub">
          We can’t send the request from here yet — this reply names the colleague but not the
          record, so there is nothing to attach the request to. Ask {held_by} directly for now.
        </p>
      )}
      {state === "asked" && (
        <p className="body jo-sub">
          {held_by} has been asked. You’ll see the candidate here once they say yes.
        </p>
      )}
      {state === "failed" && (
        <p className="body jo-detail-error" role="alert">
          We could not send that request just now. Try again in a moment.
        </p>
      )}
    </div>
  );
}

export function Field({
  label,
  required = false,
  full = false,
  children,
}: {
  label: string;
  required?: boolean;
  /** Spans both columns. For the fields a half-row misrepresents — a name, a
   *  comma-separated list, a notes box. */
  full?: boolean;
  children: ReactNode;
}) {
  return (
    <label className={full ? "cand-form-full" : undefined} style={{ display: "grid", gap: 4 }}>
      <span className="row-k">
        {label}
        {required && " *"}
      </span>
      {children}
    </label>
  );
}
