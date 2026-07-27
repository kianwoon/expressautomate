"use client";

import { useState } from "react";

import { Breakable } from "../breakable";
import { attributeLabel, useGlossary, type GlossaryCode } from "./glossary-data";
import { GlossaryForm } from "./glossary-form";

/**
 * The agency's shorthand glossary.
 *
 * Clients write `C/F`, `o/o`, `C/O` in job orders, and what those mean is not
 * universal — it is this agency's convention. So the decoding is a list the
 * agency owns and can correct, rather than something we know.
 *
 * Two origins, always visibly apart. A `starter` row is one we shipped, and we
 * have never seen this agency's email: if our guess at `o/o` is wrong, every
 * job order using it is quietly mis-read, and nothing on the screen would say
 * so. Marking the origin is how that stays honest. Editing a starter row makes
 * it the agency's own — the server does that, this page only reflects it.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export function Glossary({ enabled }: { enabled: boolean }) {
  const { state, add, edit, remove } = useGlossary(enabled);
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  return (
    <section className="alt" style={{ padding: "56px 0" }}>
      <div className="wrap" aria-live="polite">
        <span className="eyebrow">Shorthand</span>
        <h2 style={{ marginTop: 14, fontSize: "clamp(1.5rem, 2.6vw, 2rem)" }}>
          What your clients&rsquo; codes mean.
        </h2>
        <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
          When a job order contains one of these codes, we show the code as the sender wrote it
          alongside the meaning recorded here. The meaning comes from this list and nowhere else, so
          a wrong entry here is read into every email that uses that code.
        </p>

        {state.status === "loading" && (
          <p className="lede" style={{ marginTop: 24 }}>
            Looking up your glossary.
          </p>
        )}

        {/* Never "you have no codes". We could not read the list; saying it is
            empty would invite someone to add codes that already exist. */}
        {state.status === "unreadable" && (
          <p className="body gl-error" style={{ marginTop: 24 }} role="alert">
            {state.message}
          </p>
        )}

        {state.status === "ready" && (
          <>
            {state.codes.length === 0 ? (
              <p className="body muted" style={{ marginTop: 24, maxWidth: "62ch" }}>
                No codes yet. Add the first one and we will decode it wherever it appears.
              </p>
            ) : (
              <ul className="gl-list">
                {state.codes.map((entry) => (
                  <GlossaryRow
                    key={entry.id}
                    entry={entry}
                    attributes={state.attributes}
                    editing={editingId === entry.id}
                    onEdit={() => {
                      setEditingId(entry.id);
                      setAdding(false);
                    }}
                    onDone={() => setEditingId(null)}
                    onSubmit={async (draft) => {
                      const error = await edit(entry.id, draft);
                      if (!error) setEditingId(null);
                      return error;
                    }}
                    onDelete={() => remove(entry.id)}
                  />
                ))}
              </ul>
            )}

            {adding ? (
              <div className="card gl-add">
                <span className="row-k">New code</span>
                <GlossaryForm
                  attributes={state.attributes}
                  existing={null}
                  onCancel={() => setAdding(false)}
                  onSubmit={async (draft) => {
                    const error = await add(draft);
                    if (!error) setAdding(false);
                    return error;
                  }}
                />
              </div>
            ) : (
              <button
                type="button"
                className="btn btn-primary"
                style={{ marginTop: 24 }}
                onClick={() => {
                  setAdding(true);
                  setEditingId(null);
                }}
              >
                Add a code
              </button>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function GlossaryRow({
  entry,
  attributes,
  editing,
  onEdit,
  onDone,
  onSubmit,
  onDelete,
}: {
  entry: GlossaryCode;
  attributes: string[];
  editing: boolean;
  onEdit: () => void;
  onDone: () => void;
  onSubmit: React.ComponentProps<typeof GlossaryForm>["onSubmit"];
  onDelete: () => Promise<{ message: string } | null>;
}) {
  // Deleting a code stops every future email being decoded with it, so it asks
  // first — and asks in the page, where the code being removed is named and the
  // buttons are reachable by keyboard.
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const starter = entry.source === "starter";

  return (
    <li className="card gl-row" data-source={entry.source}>
      <div className="gl-row-head">
        <code className="gl-code">
          <Breakable text={entry.code} />
        </code>
        <span className="gl-arrow" aria-hidden="true">
          →
        </span>
        <span className="gl-meaning">
          <Breakable text={entry.meaning} />
        </span>
        {starter && <span className="gl-origin">Suggested by us · unchecked</span>}
      </div>

      {starter && (
        <p className="body jo-sub gl-origin-note">
          We supplied this meaning as a starting point. Nobody at your agency has confirmed it, and
          we have not seen your clients&rsquo; emails. Check it against what your clients actually
          mean — editing it makes it your agency&rsquo;s own entry.
        </p>
      )}

      <div className="rows gl-row-rows">
        <Field k="Refers to" v={entry.attribute ? attributeLabel(entry.attribute) : null} />
        <Field k="Notes" v={entry.notes} />
      </div>

      {editing ? (
        <GlossaryForm
          attributes={attributes}
          existing={entry}
          onCancel={onDone}
          onSubmit={onSubmit}
        />
      ) : confirming ? (
        <div className="gl-confirm">
          <p className="body jo-sub">
            Delete <strong>{entry.code}</strong>? Emails containing it will no longer be decoded.
            Nothing already read is changed.
          </p>
          <div className="gl-actions">
            <button
              type="button"
              className="btn btn-primary"
              disabled={deleting}
              onClick={async () => {
                setDeleting(true);
                setError(null);
                const result = await onDelete();
                if (result) {
                  setError(result.message);
                  setDeleting(false);
                  setConfirming(false);
                }
              }}
            >
              {deleting ? "Deleting…" : "Delete it"}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={deleting}
              onClick={() => setConfirming(false)}
            >
              Keep it
            </button>
          </div>
        </div>
      ) : (
        <div className="gl-actions">
          <button type="button" className="btn btn-secondary" onClick={onEdit}>
            {starter ? "Check and edit" : "Edit"}
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => setConfirming(true)}>
            Delete
          </button>
        </div>
      )}

      {error && (
        <p className="body gl-error" role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

function Field({ k, v }: { k: string; v: string | null }) {
  return (
    <div className="row">
      <span className="row-k">{k}</span>
      <span className={v ? undefined : "muted"}>{v ? <Breakable text={v} /> : "Not mentioned"}</span>
    </div>
  );
}
