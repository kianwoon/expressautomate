"use client";

import { useId, useState } from "react";

import { attributeLabel, type CodeDraft, type GlossaryCode, type WriteError } from "./glossary-data";

/**
 * Adding a code, and editing one — the same four fields either way.
 *
 * The code itself is only editable when it does not exist yet. Once a code is
 * in the glossary it is the string a client actually typed in an email, and
 * renaming it would re-point every past decoding at wording nobody sent.
 *
 * The attribute list is whatever the server sent. Hardcoding it here would put
 * a second, quietly diverging copy of that vocabulary in the browser.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

const NONE = "";

export function GlossaryForm({
  attributes,
  existing,
  onSubmit,
  onCancel,
}: {
  attributes: string[];
  /** The row being edited, or null when this is the add form. */
  existing: GlossaryCode | null;
  onSubmit: (draft: CodeDraft) => Promise<WriteError | null>;
  onCancel: () => void;
}) {
  const ids = useId();
  const [code, setCode] = useState(existing?.code ?? "");
  const [meaning, setMeaning] = useState(existing?.meaning ?? "");
  const [attribute, setAttribute] = useState(existing?.attribute ?? NONE);
  const [notes, setNotes] = useState(existing?.notes ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<WriteError | null>(null);

  const trimmedCode = code.trim();
  const trimmedMeaning = meaning.trim();
  const ready = trimmedMeaning.length > 0 && (existing !== null || trimmedCode.length > 0);

  async function submit(event: React.FormEvent) {
    // The form element is what makes Enter submit; this stops the page reload
    // that would otherwise come with it.
    event.preventDefault();
    if (!ready || saving) return;
    setSaving(true);
    setError(null);
    const result = await onSubmit({
      ...(existing ? {} : { code: trimmedCode }),
      meaning: trimmedMeaning,
      attribute: attribute === NONE ? null : attribute,
      notes: notes.trim() === "" ? null : notes.trim(),
    });
    if (result) {
      setError(result);
      setSaving(false);
      return;
    }
    // On success the parent closes this form, so no state is reset here — the
    // component is about to go away.
  }

  return (
    <form className="gl-form" onSubmit={submit}>
      <div className="gl-form-grid">
        <label className="gl-field" htmlFor={`${ids}-code`}>
          <span className="row-k">Code</span>
          <input
            id={`${ids}-code`}
            className="gl-input"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            readOnly={existing !== null}
            required
            autoComplete="off"
            placeholder="C/F"
            aria-describedby={existing ? `${ids}-code-note` : undefined}
          />
          {existing && (
            <span className="body jo-sub" id={`${ids}-code-note`}>
              The code is what the sender wrote. It cannot be changed.
            </span>
          )}
        </label>

        <label className="gl-field" htmlFor={`${ids}-meaning`}>
          <span className="row-k">Means</span>
          <input
            id={`${ids}-meaning`}
            className="gl-input"
            value={meaning}
            onChange={(event) => setMeaning(event.target.value)}
            required
            autoComplete="off"
            placeholder="Chinese female"
          />
        </label>

        <label className="gl-field" htmlFor={`${ids}-attribute`}>
          <span className="row-k">Refers to</span>
          <select
            id={`${ids}-attribute`}
            className="gl-input"
            value={attribute}
            onChange={(event) => setAttribute(event.target.value)}
          >
            <option value={NONE}>Nothing protected</option>
            {attributes.map((name) => (
              <option key={name} value={name}>
                {attributeLabel(name)}
              </option>
            ))}
          </select>
        </label>

        <label className="gl-field gl-field-wide" htmlFor={`${ids}-notes`}>
          <span className="row-k">Notes</span>
          <input
            id={`${ids}-notes`}
            className="gl-input"
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            autoComplete="off"
            placeholder="Optional — where you have seen this used"
          />
        </label>
      </div>

      <div className="gl-form-actions">
        <button className="btn btn-primary" type="submit" disabled={!ready || saving}>
          {saving ? "Saving…" : existing ? "Save changes" : "Add code"}
        </button>
        <button className="btn btn-secondary" type="button" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
        {existing?.source === "starter" && (
          <p className="body jo-sub gl-form-hint">
            Saving this makes it your agency&rsquo;s own entry rather than one we suggested.
          </p>
        )}
      </div>

      {/* A refused write says exactly what was refused. The 409 in particular
          carries the meaning already stored under that code, which is the only
          thing that turns "that failed" into something a person can act on. */}
      {error && (
        <p className="body gl-error" role="alert">
          {error.message}
        </p>
      )}
    </form>
  );
}
