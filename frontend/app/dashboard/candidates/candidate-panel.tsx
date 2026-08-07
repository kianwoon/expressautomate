"use client";

import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";

import { CANDIDATES_PATH } from "../../api";
import { useAuth } from "../../auth";
import type { Candidate, CandidateCollision, CandidatePage } from "../candidates";
import {
  ApiError,
  canEditCandidate,
  CandidateCollisionError,
  claimCandidate,
  mergeCandidate,
  unmergeCandidate,
  updateCandidate,
} from "../candidates";
import { CandidateCv } from "./candidate-cv";
import { CandidateHistory } from "./candidate-history";
import { CandidateShareDialog } from "./candidate-share";
import { WhatsappActivityTimeline, WhatsappButton } from "./candidate-whatsapp";
import {
  changedFields,
  Field,
  FormState,
  HeldByColleague,
  NATIONALITY_HINTS,
  RACES,
  SEXES,
  STAGES,
  toFormState,
  toSubmitBody,
} from "./candidate-form";
import { Dialog } from "../dialog";

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

/** The action row's glyphs.
 *
 *  WhatsApp went to an icon first and left the words beside it looking like a
 *  different kind of control; these follow it so the row reads as one set. Each
 *  shape is the one the reader has already learnt somewhere else — connected
 *  nodes for share, a lidded box for archive, a bin for delete, an arrow going
 *  back for restore — because a glyph nobody recognises is strictly worse than
 *  the word it replaced.
 *
 *  They are drawn to one specification, and the first attempt was not: an
 *  18px hairline adrift in a 42px tile, beside a WhatsApp mark that fills its
 *  own tile corner to corner. Three things have to agree or the row reads as
 *  three unrelated controls —
 *
 *    - the same optical box. Every glyph is drawn to roughly 3.5 → 20.5 in
 *      this 24 grid, so the bin does not sit smaller than the archive box
 *      merely because a bin is a narrower object.
 *    - the same stroke. 2.2 rather than 1.8: the weight that stands up next
 *      to a solid brand mark instead of fading beside it.
 *    - the same corner. 1.2–1.4 radii throughout, matching each other rather
 *      than whatever each shape happened to want.
 *
 *  `currentColor` and no size attribute: `.btn-secondary` supplies the ink,
 *  and `.action-glyph` in `app.css` supplies the 24px — larger than the 18px
 *  `.btn-icon` gives a text-sized icon, because these stand beside a mark that
 *  is the full height of the button.
 *
 *  `aria-hidden`, because the accessible name lives on the button — the same
 *  arrangement as `WhatsappGlyph`.
 *
 *  allow-hardcode: the `d` attributes below are glyph outlines — drawn shapes,
 *  not a list of inputs anything is matched against. There is no
 *  content-agnostic way to express a bin. */
function ActionGlyph({ children }: { children: ReactNode }) {
  return (
    <svg
      className="action-glyph"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const SHARE_GLYPH = (
  <ActionGlyph>
    <circle cx="17.6" cy="6.2" r="2.6" />
    <circle cx="6.4" cy="12" r="2.6" />
    <circle cx="17.6" cy="17.8" r="2.6" />
    <path d="M8.7 10.8 15.3 7.4" />
    <path d="M8.7 13.2 15.3 16.6" />
  </ActionGlyph>
);

const ARCHIVE_GLYPH = (
  <ActionGlyph>
    <rect x="3.2" y="4.2" width="17.6" height="4.4" rx="1.4" />
    <path d="M5.4 8.6v10.2a1.4 1.4 0 0 0 1.4 1.4h10.4a1.4 1.4 0 0 0 1.4-1.4V8.6" />
    <path d="M9.8 13h4.4" />
  </ActionGlyph>
);

const RESTORE_GLYPH = (
  <ActionGlyph>
    <path d="M3.6 2.7v5.6h5.6" />
    <path d="M4.1 9.2a8.4 8.4 0 1 0 2-2.9L3.6 8.3" />
  </ActionGlyph>
);

const DELETE_GLYPH = (
  <ActionGlyph>
    <path d="M3.8 6.6h16.4" />
    <path d="M9.4 6.6V4.8a1.2 1.2 0 0 1 1.2-1.2h2.8a1.2 1.2 0 0 1 1.2 1.2v1.8" />
    <path d="M5.9 6.6v12.3a1.4 1.4 0 0 0 1.4 1.4h9.4a1.4 1.4 0 0 0 1.4-1.4V6.6" />
    <path d="M10.2 10.8v5.4" />
    <path d="M13.8 10.8v5.4" />
  </ActionGlyph>
);

const STAGE_LABEL: Record<Candidate["pipeline_stage"], string> = {
  new: "New",
  contacted: "Contacted",
  submitted: "Submitted",
  placed: "Placed",
  rejected: "Rejected",
};

export function CandidatePanel({
  row,
  onClose,
  onArchive,
  onRestore,
  onDelete,
  onChanged,
  onDetailChanged,
}: {
  row: Candidate | null;
  /** Closes the modal. The parent owns the `selectedId`; this just clears it. */
  onClose: () => void;
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
  if (!row) return null;

  return (
    <Detail
      key={row.id}
      row={row}
      onClose={onClose}
      onArchive={onArchive}
      onRestore={onRestore}
      onDelete={onDelete}
      onChanged={onChanged}
      onDetailChanged={onDetailChanged}
    />
  );
}

function Detail({
  row,
  onClose,
  onArchive,
  onRestore,
  onDelete,
  onChanged,
  onDetailChanged,
}: {
  row: Candidate;
  onClose: () => void;
  onArchive: () => Promise<void>;
  onRestore: () => Promise<void>;
  onDelete: (() => Promise<void>) | null;
  onChanged: () => void;
  onDetailChanged: () => void;
}) {
  const auth = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  // Bumped after a WhatsApp open is logged, so the activity timeline below
  // refetches even though the candidate's own id hasn't changed.
  const [activityVersion, setActivityVersion] = useState(0);

  // Inline-editable fields, edit-on-by-default. The form state mirrors
  // `CandidateForm`'s exactly (same helpers), so a save sends only the fields
  // that actually changed — a typo fix does not override-protect the other
  // thirteen. `initial` is captured at mount and reset after each successful
  // save, so the diff is always against the last saved record.
  const [form, setForm] = useState<FormState>(() => toFormState(row));
  const [initial, setInitial] = useState<FormState>(() => toFormState(row));
  const [savingFields, setSavingFields] = useState(false);
  const [collision, setCollision] = useState<CandidateCollision | null>(null);

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  async function saveFields() {
    if (savingFields) return;
    setSavingFields(true);
    setError(null);
    setCollision(null);
    try {
      const body = changedFields(toSubmitBody(form), initial);
      await updateCandidate(row.id, body);
      // The diff against the last saved state, so the next save compares to
      // this one rather than to the original record.
      setInitial(form);
      onDetailChanged();
    } catch (err) {
      if (err instanceof CandidateCollisionError) {
        setCollision(err.collision);
      } else {
        setError(err instanceof ApiError ? err.message : "We could not save that just now.");
      }
    } finally {
      setSavingFields(false);
    }
  }

  const signedIn = auth.status === "signed-in" ? auth.me.user : null;
  // The server's rule, published on the row — not re-derived from `owner`
  // and a role check, which is a second place for the two to drift. A
  // signed-out reader cannot be here at all — the page redirects — so `null`
  // is the momentary gap before the session resolves; `can_edit` on `row`
  // already answers the question for whoever the server authenticated this
  // fetch as, so there is nothing further to gate on `signedIn` here.
  const canEdit = signedIn === null || canEditCandidate(row);
  const unclaimed = row.owner === null;
  const ownerName = row.owner === null ? null : (row.owner.name ?? "A colleague");

  async function claim() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await claimCandidate(row.id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not claim that just now.");
    } finally {
      setBusy(false);
    }
  }

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
    <Dialog
      titleId="cand-detail-title"
      onClose={busy ? () => {} : onClose}
      className="dlg-modal-wide cand-detail-modal"
      title={
        row.record_status === "merged"
          ? `${row.full_name} · Merged`
          : `${row.full_name} · ${STAGE_LABEL[row.pipeline_stage]}`
      }
    >
      {row.record_status === "merged" ? (
        <MergedInto row={row} onUnmerge={unmerge} busy={busy} />
      ) : (
        <>
          {/* Editable fields in the same multi-column grid the create form
              uses (`.cand-form`), not the single-column `.rows` ribbon: a
              wide modal with 15+ fields stacked one-per-row was a tall scroll
              nobody could scan. The grid flows fields into two columns and
              `.cand-form-full` spans the rows that should not be split (name,
              skills, notes). The override ✎ marker rides on the label. */}
          <div className="cand-form" style={{ marginTop: 16 }}>
            <Field label="Full name" full>
              <input
                className="jo-search"
                value={form.full_name}
                onChange={(e) => set("full_name", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            {/* Owner is read-only — not a candidate-editable field. Full width
                so the "Unclaimed" sentence is not cut at a column break. */}
            <div className="cand-form-full" style={{ display: "grid", gap: 4 }}>
              <span className="row-k">Owner</span>
              <span className={ownerName ? "body" : "body muted"}>
                {ownerName ?? "Unclaimed — anyone at the agency can take this one"}
              </span>
            </div>
            <Field label="Email">
              <input
                className="jo-search"
                type="email"
                value={form.email}
                onChange={(e) => set("email", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Phone">
              <input
                className="jo-search"
                value={form.phone_raw}
                onChange={(e) => set("phone_raw", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Current title">
              <input
                className="jo-search"
                value={form.current_title}
                onChange={(e) => set("current_title", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Current employer">
              <input
                className="jo-search"
                value={form.current_employer}
                onChange={(e) => set("current_employer", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Location">
              <input
                className="jo-search"
                value={form.location}
                onChange={(e) => set("location", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Stage">
              <select
                className="jo-search"
                value={form.pipeline_stage}
                onChange={(e) => set("pipeline_stage", e.target.value as Candidate["pipeline_stage"])}
                disabled={!canEdit || savingFields}
              >
                {STAGES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Years of experience">
              <input
                className="jo-search"
                type="number"
                value={form.years_experience}
                onChange={(e) => set("years_experience", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Expected salary">
              <input
                className="jo-search"
                type="number"
                value={form.expected_salary}
                onChange={(e) => set("expected_salary", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Salary currency">
              <input
                className="jo-search"
                value={form.salary_currency}
                onChange={(e) => set("salary_currency", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Salary period">
              <input
                className="jo-search"
                value={form.salary_period}
                onChange={(e) => set("salary_period", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Available from">
              <input
                className="jo-search"
                type="date"
                value={form.available_from}
                onChange={(e) => set("available_from", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Notice period">
              <input
                className="jo-search"
                value={form.notice_period_raw}
                onChange={(e) => set("notice_period_raw", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Employment type">
              <input
                className="jo-search"
                value={form.employment_type}
                onChange={(e) => set("employment_type", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Skills (comma separated)" full>
              <input
                className="jo-search"
                value={form.skills}
                onChange={(e) => set("skills", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Notes" full>
              <textarea
                className="jo-search"
                style={{ minHeight: 72 }}
                value={form.notes}
                onChange={(e) => set("notes", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
          </div>

          {/* The two regulatory fieldsets, the same markup the create form
              uses. They share a row (`.cand-group-permit` spans 2, `.cand-group-
              record` spans 1) so the modal does not grow by their combined
              height. Edit-on-by-default means these are reachable without
              opening a separate form; the PDPA framing carries over verbatim. */}
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
                disabled={!canEdit || savingFields}
              >
                <option value="">Not recorded</option>
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
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Years of formal education">
              <input
                className="jo-search"
                type="number"
                value={form.education_years}
                onChange={(e) => set("education_years", e.target.value)}
                disabled={!canEdit || savingFields}
              />
            </Field>
            <Field label="Nationality">
              <input
                className="jo-search"
                list="cand-detail-nationality-hints"
                value={form.nationality}
                onChange={(e) => set("nationality", e.target.value)}
                disabled={!canEdit || savingFields}
                placeholder="Two-letter country code, e.g. PH"
              />
              <datalist id="cand-detail-nationality-hints">
                {NATIONALITY_HINTS.map((c) => (
                  <option key={c.code} value={c.code}>
                    {c.name}
                  </option>
                ))}
              </datalist>
            </Field>
          </fieldset>

          <fieldset className="cand-group cand-group-record">
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
                disabled={!canEdit || savingFields}
              >
                <option value="">Not recorded</option>
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
                  disabled={!canEdit || savingFields}
                />
              </Field>
            )}
          </fieldset>

          {/* Below the fields rather than above them, because three of those
              fields — title, employer, experience — are derived from these
              rows. The reader meets the summary first and then what it was
              computed from. */}
          {/* Above the history, because it is what fills the history in: the
              reader meets the CV, then the roles read out of it, then the
              summary those roles were derived from. */}
          <CandidateCv row={row} onChanged={onDetailChanged} />

          <CandidateHistory row={row} onChanged={onDetailChanged} />

          <WhatsappActivityTimeline row={row} version={activityVersion} />

          <MergePicker candidateId={row.id} onMerged={onChanged} />
        </>
      )}

      <div className="jo-detail-actions">
        <div className="jo-action-row">
          {/* Save replaces the old Edit button: editing is inline now, so the
              action a recruiter takes on this modal is "save the fields I
              changed", not "open a separate form". Disabled (never hidden) for
              a share recipient exactly as Edit was — a vanished button reads
              as a page that failed to load. */}
          <button
            type="button"
            className="btn btn-primary"
            onClick={saveFields}
            disabled={busy || !canEdit || savingFields}
            title={canEdit ? undefined : "Only the recruiter who holds this candidate can edit it."}
          >
            {savingFields ? "Saving…" : "Save changes"}
          </button>
          {/* Icon-only from here down, and the name has to come from
              `aria-label` — which doubles as the hover tooltip, so a sighted
              reader who does not recognise the shape is one hover from the
              word. */}
          {row.record_status !== "merged" && (
            <button
              type="button"
              className="btn btn-secondary btn-icon"
              onClick={() => setSharing(true)}
              disabled={busy}
              aria-label="Share"
              title="Share"
            >
              {SHARE_GLYPH}
            </button>
          )}
          {/* Claiming is what CREATES the right to edit, so it is offered
              exactly where editing is refused for want of an owner — and it is
              offered to everyone, because an unclaimed candidate is queue work
              the whole agency can already see. */}
          {unclaimed && row.record_status === "active" && (
            <button type="button" className="btn btn-secondary" onClick={claim} disabled={busy}>
              {busy ? "Saving…" : "Claim"}
            </button>
          )}
          {row.record_status !== "merged" && (
            <WhatsappButton row={row} onLogged={() => setActivityVersion((v) => v + 1)} />
          )}
          {row.record_status === "active" && (
            <button
              type="button"
              className="btn btn-secondary btn-icon"
              onClick={archive}
              disabled={busy}
              // The name stays "Archive" while the request is in flight.
              // Swapping it to "Saving…" swapped the button's identity out of
              // the accessibility tree — and a screen reader does not
              // re-announce a name change anyway, so the swap cost the label
              // and bought nothing. `aria-busy` is the part that carries.
              aria-label="Archive"
              title={busy ? "Saving…" : "Archive"}
              aria-busy={busy}
            >
              {ARCHIVE_GLYPH}
            </button>
          )}
          {row.record_status === "archived" && (
            <button
              type="button"
              className="btn btn-secondary btn-icon"
              onClick={restore}
              disabled={busy}
              aria-label="Restore"
              title={busy ? "Saving…" : "Restore"}
              aria-busy={busy}
            >
              {RESTORE_GLYPH}
            </button>
          )}
          {onDelete && (
            <button
              type="button"
              className="btn btn-secondary btn-icon"
              onClick={remove}
              disabled={busy}
              aria-label="Delete"
              title="Delete"
            >
              {DELETE_GLYPH}
            </button>
          )}
        </div>
        {!canEdit && (
          <p className="body jo-sub">
            {unclaimed
              ? "Nobody holds this candidate yet. Claim them to edit the record — an unclaimed record is where a wrong edit is least likely to be noticed, so the server refuses it too."
              : `${ownerName ?? "A colleague"} holds this candidate. You can read the record, log a WhatsApp message against it, and pass it on to someone else — but only they can change it.`}
          </p>
        )}
      </div>

      {sharing && <CandidateShareDialog row={row} onClose={() => setSharing(false)} />}

      {collision && <HeldByColleague collision={collision} />}

      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </Dialog>
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
