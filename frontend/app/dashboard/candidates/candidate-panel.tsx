"use client";

import { useEffect, useRef, useState } from "react";

import { Breakable } from "../../breakable";
import { CANDIDATES_PATH } from "../../api";
import type { Candidate, CandidatePage } from "../candidates";
import { mergeCandidate, unmergeCandidate } from "../candidates";
import { Value, day } from "../format";
import { CandidateAvatar } from "./candidate-avatar";
import { CandidateHistory } from "./candidate-history";

/**
 * One candidate in full, beside the list.
 *
 * A candidate-specific sibling of `detail-panel.tsx`, written beside it
 * rather than folded into it: the fields, the actions (merge, unmerge,
 * archive, delete) and the override marker are all specific to a person's
 * record and have no equivalent on a job order.
 *
 * A field the recruiter corrected by hand is marked, not just shown — that
 * marker is the only reason `overridden_fields` exists: it tells the reader
 * an import will not silently overwrite what they typed.
 */

const STAGE_LABEL: Record<Candidate["pipeline_stage"], string> = {
  new: "New",
  contacted: "Contacted",
  submitted: "Submitted",
  placed: "Placed",
  rejected: "Rejected",
};

export function CandidatePanel({
  row,
  onEdit,
  onArchive,
  onRestore,
  onDelete,
  onChanged,
  onDetailChanged,
}: {
  row: Candidate | null;
  onEdit: () => void;
  onArchive: () => Promise<void>;
  /** Undoes an archive. Archiving is reversible by design, so this is offered
   *  wherever Archive is, just on the other side of the same toggle. */
  onRestore: () => Promise<void>;
  /** `null` for a recruiter — the button is absent rather than present and
   *  guaranteed to 403. */
  onDelete: (() => Promise<void>) | null;
  /** Called after a merge or unmerge succeeds, so the caller can refetch the
   *  list and the detail record. */
  onChanged: () => void;
  /** Refetch this candidate alone; do not reload the list. Serves both the
   *  photo and the work history — neither changes anything the table draws,
   *  and re-reading the list for them is what used to blank the screen
   *  mid-upload. */
  onDetailChanged: () => void;
}) {
  if (!row) {
    return (
      <aside className="card jo-detail" aria-label="Candidate details">
        <span className="eyebrow">Details</span>
        <p className="body jo-detail-empty">
          Select a candidate to read their full record, including skills and any fields edited by
          hand.
        </p>
      </aside>
    );
  }

  return (
    <Detail
      key={row.id}
      row={row}
      onEdit={onEdit}
      onArchive={onArchive}
      onRestore={onRestore}
      onDelete={onDelete}
      onChanged={onChanged}
      onDetailChanged={onDetailChanged}
    />
  );
}

function overridden(row: Candidate, field: string): boolean {
  return (row.overridden_fields ?? []).includes(field);
}

function Detail({
  row,
  onEdit,
  onArchive,
  onRestore,
  onDelete,
  onChanged,
  onDetailChanged,
}: {
  row: Candidate;
  onEdit: () => void;
  onArchive: () => Promise<void>;
  onRestore: () => Promise<void>;
  onDelete: (() => Promise<void>) | null;
  onChanged: () => void;
  onDetailChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function archive() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onArchive();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not save that just now.");
    } finally {
      setBusy(false);
    }
  }

  async function restore() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onRestore();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not save that just now.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (busy || !onDelete) return;
    if (!window.confirm(`Delete ${row.full_name}? This cannot be undone.`)) return;
    setBusy(true);
    setError(null);
    try {
      await onDelete();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not delete that just now.");
      setBusy(false);
    }
  }

  async function unmerge() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await unmergeCandidate(row.id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not unmerge that just now.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="card jo-detail" aria-label="Candidate details">
      <div className="jo-detail-head">
        <span className="eyebrow">Details</span>
        {row.record_status === "merged" ? (
          <span className="muted">Merged</span>
        ) : (
          <span className="eyebrow">{STAGE_LABEL[row.pipeline_stage]}</span>
        )}
      </div>

      {/* The name sits beside the photo rather than under it: the two
          together are the answer to "who am I looking at", and splitting
          them left the circle paired with a sentence about uploading. */}
      <CandidateAvatar row={row} onChanged={onDetailChanged}>
        <h3 className="jo-detail-title ca-name">{row.full_name}</h3>
        {row.current_title && (
          <p className="jo-detail-company">
            <Value text={row.current_title} />
            {row.current_employer && <> at {row.current_employer}</>}
          </p>
        )}
      </CandidateAvatar>

      {row.record_status === "merged" ? (
        <MergedInto row={row} onUnmerge={unmerge} busy={busy} />
      ) : (
        <>
          <div className="rows jo-detail-rows">
            <OverrideRow row={row} field="email" k="Email" v={row.email} />
            <OverrideRow row={row} field="phone_raw" k="Phone" v={row.phone_raw} />
            <OverrideRow row={row} field="location" k="Location" v={row.location} />
            <OverrideRow
              row={row}
              field="years_experience"
              k="Experience"
              v={row.years_experience != null ? `${row.years_experience} years` : null}
            />
            <OverrideRow
              row={row}
              field="expected_salary"
              k="Expected salary"
              v={
                row.expected_salary != null
                  ? `${row.salary_currency ?? ""} ${row.expected_salary}${
                      row.salary_period ? ` per ${row.salary_period}` : ""
                    }`.trim()
                  : null
              }
            />
            <OverrideRow row={row} field="available_from" k="Available from" v={day(row.available_from)} />
            <OverrideRow row={row} field="notice_period_raw" k="Notice period" v={row.notice_period_raw} />
            <OverrideRow row={row} field="employment_type" k="Employment type" v={row.employment_type} />
          </div>

          {row.skills && row.skills.length > 0 && (
            <div className="jo-detail-prose">
              <span className="row-k">Skills</span>
              <p className="body">{row.skills.join(", ")}</p>
            </div>
          )}

          <div className="jo-detail-prose">
            <span className="row-k">Notes</span>
            <OverrideProse row={row} field="notes" text={row.notes} />
          </div>

          {/* Below the fields rather than above them, because three of those
              fields — title, employer, experience — are derived from these
              rows. The reader meets the summary first and then what it was
              computed from. */}
          <CandidateHistory row={row} onChanged={onDetailChanged} />

          <MergePicker candidateId={row.id} onMerged={onChanged} />
        </>
      )}

      <div className="jo-detail-actions">
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <button type="button" className="btn btn-primary" onClick={onEdit} disabled={busy}>
            Edit
          </button>
          {row.record_status === "active" && (
            <button type="button" className="btn btn-secondary" onClick={archive} disabled={busy}>
              {busy ? "Saving…" : "Archive"}
            </button>
          )}
          {row.record_status === "archived" && (
            <button type="button" className="btn btn-secondary" onClick={restore} disabled={busy}>
              {busy ? "Saving…" : "Restore"}
            </button>
          )}
          {onDelete && (
            <button type="button" className="btn btn-secondary" onClick={remove} disabled={busy}>
              Delete
            </button>
          )}
        </div>
      </div>

      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </aside>
  );
}

/** "Edited by hand — an import will not change this" is the whole reason the
 *  overrides table exists, and is invisible without this marker. */
function OverrideRow({
  row,
  field,
  k,
  v,
}: {
  row: Candidate;
  field: string;
  k: string;
  v: string | null;
}) {
  const marked = overridden(row, field);
  return (
    <div className="row">
      <span className="row-k">
        {k}
        {marked && (
          <span
            title="Edited by hand — an import will not change this"
            style={{ marginLeft: 6, cursor: "help" }}
            aria-label="Edited by hand — an import will not change this"
          >
            ✎
          </span>
        )}
      </span>
      <span className={v ? undefined : "muted"}>{v ? <Breakable text={v} /> : "Not mentioned"}</span>
    </div>
  );
}

function OverrideProse({ row, field, text }: { row: Candidate; field: string; text: string | null }) {
  const marked = overridden(row, field);
  return (
    <p className={text ? "body" : "body muted"}>
      {marked && (
        <span
          title="Edited by hand — an import will not change this"
          style={{ marginRight: 6, cursor: "help" }}
          aria-label="Edited by hand — an import will not change this"
        >
          ✎
        </span>
      )}
      {text ? <Breakable text={text} /> : "Not mentioned"}
    </p>
  );
}

function MergedInto({
  row,
  onUnmerge,
  busy,
}: {
  row: Candidate;
  onUnmerge: () => void;
  busy: boolean;
}) {
  return (
    <div className="jo-detail-rows rows">
      <p className="body">
        This record was merged into another candidate
        {row.merged_into_candidate_id && (
          <>
            {" "}
            (id <code>{row.merged_into_candidate_id}</code>)
          </>
        )}
        . Their details now hold everything this person had.
      </p>
      <button type="button" className="btn btn-secondary" onClick={onUnmerge} disabled={busy}>
        {busy ? "Working…" : "Unmerge"}
      </button>
    </div>
  );
}

/** Opens a search over the same `q` endpoint the list uses, and merges the
 *  current candidate into whichever result is chosen. The only way into
 *  `POST /candidates/{id}/merge` — without it a duplicate is unreachable
 *  except by curl. */
function MergePicker({ candidateId, onMerged }: { candidateId: string; onMerged: () => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Candidate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open) return;
    const needle = q.trim();
    if (!needle) {
      setResults([]);
      return;
    }
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const timer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({ q: needle, limit: "10" });
        const res = await fetch(`${CANDIDATES_PATH}?${params.toString()}`, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) return;
        const body = (await res.json()) as CandidatePage;
        setResults(body.items.filter((c) => c.id !== candidateId));
      } catch {
        /* the box will simply show nothing; the search box itself is not
           worth an error banner over */
      }
    }, 200);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [open, q, candidateId]);

  async function choose(targetId: string) {
    setBusy(true);
    setError(null);
    try {
      await mergeCandidate(candidateId, targetId);
      setOpen(false);
      setQ("");
      setResults([]);
      onMerged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not merge that just now.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div className="jo-detail-prose">
        <button type="button" className="btn btn-secondary" onClick={() => setOpen(true)}>
          Merge into…
        </button>
      </div>
    );
  }

  return (
    <div className="jo-detail-prose">
      <span className="row-k">Merge into</span>
      <input
        className="jo-search"
        type="search"
        autoFocus
        value={q}
        onChange={(event) => setQ(event.target.value)}
        placeholder="Search by name, email or phone…"
        aria-label="Search for the candidate to merge into"
        style={{ marginTop: 8 }}
      />
      {results.length > 0 && (
        <ul style={{ marginTop: 8, listStyle: "none", padding: 0 }}>
          {results.map((c) => (
            <li key={c.id} style={{ marginTop: 4 }}>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => choose(c.id)}
                style={{ width: "100%", textAlign: "left" }}
              >
                {c.full_name}
                {c.email && <span className="muted"> — {c.email}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
        <button type="button" className="btn btn-secondary" onClick={() => setOpen(false)} disabled={busy}>
          Cancel
        </button>
      </div>
      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
