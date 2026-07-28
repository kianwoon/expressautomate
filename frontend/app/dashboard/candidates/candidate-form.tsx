"use client";

import { useState, type FormEvent, type ReactNode } from "react";

import { ApiError, createCandidate, updateCandidate, type Candidate, type Stage } from "../candidates";

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

const STAGES: { value: Stage; label: string }[] = [
  { value: "new", label: "New" },
  { value: "contacted", label: "Contacted" },
  { value: "submitted", label: "Submitted" },
  { value: "placed", label: "Placed" },
  { value: "rejected", label: "Rejected" },
];

type FormState = {
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
};

function toFormState(row: Candidate | null): FormState {
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
  };
}

/** Blank strings become `null`, not `""` — an empty string is a value the
 *  email or the recruiter stated; the backend's `Not mentioned` convention
 *  means absent fields must actually be absent. */
function orNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
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
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  const canSubmit = form.full_name.trim() !== "" && !saving;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setSaving(true);
    setError(null);

    const body = {
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
    };

    try {
      const saved = row ? await updateCandidate(row.id, body) : await createCandidate(body);
      onDone(saved);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We could not save that just now.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card" style={{ padding: 24, maxWidth: 560 }}>
      <span className="eyebrow">{row ? "Edit candidate" : "Add candidate"}</span>

      {error && (
        <p className="body jo-detail-error" role="alert" style={{ marginTop: 12 }}>
          {error}
        </p>
      )}

      <form onSubmit={submit} style={{ marginTop: 16, display: "grid", gap: 12 }}>
        <Field label="Full name" required>
          <input
            className="jo-search"
            value={form.full_name}
            onChange={(e) => set("full_name", e.target.value)}
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
          />
        </Field>
        <Field label="Phone">
          <input
            className="jo-search"
            value={form.phone_raw}
            onChange={(e) => set("phone_raw", e.target.value)}
          />
        </Field>
        <Field label="Current title">
          <input
            className="jo-search"
            value={form.current_title}
            onChange={(e) => set("current_title", e.target.value)}
          />
        </Field>
        <Field label="Current employer">
          <input
            className="jo-search"
            value={form.current_employer}
            onChange={(e) => set("current_employer", e.target.value)}
          />
        </Field>
        <Field label="Location">
          <input
            className="jo-search"
            value={form.location}
            onChange={(e) => set("location", e.target.value)}
          />
        </Field>
        <Field label="Years of experience">
          <input
            className="jo-search"
            type="number"
            value={form.years_experience}
            onChange={(e) => set("years_experience", e.target.value)}
          />
        </Field>
        <Field label="Expected salary">
          <input
            className="jo-search"
            type="number"
            value={form.expected_salary}
            onChange={(e) => set("expected_salary", e.target.value)}
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
          />
        </Field>
        <Field label="Employment type">
          <input
            className="jo-search"
            value={form.employment_type}
            onChange={(e) => set("employment_type", e.target.value)}
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
        <Field label="Skills (comma separated)">
          <input
            className="jo-search"
            value={form.skills}
            onChange={(e) => set("skills", e.target.value)}
          />
        </Field>
        <Field label="Notes">
          <textarea
            className="jo-search"
            style={{ minHeight: 80 }}
            value={form.notes}
            onChange={(e) => set("notes", e.target.value)}
          />
        </Field>

        <div style={{ display: "flex", gap: 10, marginTop: 8 }}>
          <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
            {saving ? "Saving…" : row ? "Save changes" : "Add candidate"}
          </button>
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={saving}>
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}

function Field({
  label,
  required = false,
  children,
}: {
  label: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <label style={{ display: "grid", gap: 4 }}>
      <span className="row-k">
        {label}
        {required && " *"}
      </span>
      {children}
    </label>
  );
}
