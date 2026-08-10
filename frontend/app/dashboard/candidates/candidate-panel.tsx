"use client";

import type { ReactNode } from "react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

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
import {
  findCandidateJobs,
  type CandidateJobMatch,
  type CandidateJobReason,
  type CandidateJobs,
} from "../candidate-jobs";
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
  SalaryGroup,
  SEXES,
  STAGES,
  toFormState,
  toSubmitBody,
} from "./candidate-form";
import { Dialog } from "../dialog";
import { useCandidateIntelligence } from "../candidate-intelligence";
import {
  AssessmentStage,
  EducationStage,
  WorkStage,
  type CandidateStageState,
} from "../candidate-intelligence-panel";

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

/** The candidate modal's tabs. Details holds the existing editable record;
 *  the other three are the Candidate Intelligence stages — the same shape the
 *  job-order modal's Origin/Work/Person/Search tabs take — and Jobs is the
 *  "Find Job" shortlist of best-fitting job orders. */
type CandidateTab = "details" | "assessment" | "work" | "education" | "jobs";

const CANDIDATE_TABS: { key: CandidateTab; label: string }[] = [
  { key: "details", label: "Details" },
  { key: "assessment", label: "Assessment" },
  { key: "work", label: "Work" },
  { key: "education", label: "Education" },
  { key: "jobs", label: "Jobs" },
];

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

  // Candidate Intelligence: the analysis runs as a background job, exactly as
  // the Job Intelligence analysis does on the job-order modal. The hook owns
  // the run/starting/waiting/poll state; the header button calls `ci.run()`
  // and the three stage tabs read `ci.analysis`. Called once here so the
  // header and the tabs share one source of truth.
  const ci = useCandidateIntelligence(row.id);
  const [activeTab, setActiveTab] = useState<CandidateTab>("details");

  // Find Job: the best-fitting job orders, scored server-side on demand (pure
  // arithmetic over the candidate's profile — nothing to poll or re-read). The
  // result survives tab switches; the button re-runs and replaces it, so a
  // recruiter who edits the profile can ask again.
  const [jobs, setJobs] = useState<CandidateJobs | null>(null);
  const [jobsLoading, setJobsLoading] = useState(false);
  const [jobsError, setJobsError] = useState<string | null>(null);

  // The Details tab is the tallest (it carries the editable fields, activity
  // timeline and merge picker). Once measured, its height is locked as the
  // panel's min-height so switching to a shorter Career/Capability/Profile tab
  // does not collapse the modal — every tab renders against the same canvas,
  // and the height stops jumping. A ResizeObserver tracks Details' height as
  // it settles (the activity timeline arrives after mount and grows the panel),
  // and only while Details is the active tab. The locked height persists across
  // tab switches. Mirrors the same mechanism in `detail-panel.tsx`.
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [panelMinHeight, setPanelMinHeight] = useState<number | null>(null);
  useLayoutEffect(() => {
    if (activeTab !== "details" || !panelRef.current) return;
    const el = panelRef.current;
    const measure = () => {
      const h = el.scrollHeight;
      if (h > 0) setPanelMinHeight(h);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [activeTab]);

  async function runAnalysis() {
    await ci.run();
    // Land on the Assessment tab — the sharp headline read.
    setActiveTab("assessment");
  }

  async function findJobs() {
    if (jobsLoading) return;
    setJobsLoading(true);
    setJobsError(null);
    try {
      setJobs(await findCandidateJobs(row.id));
      // Land on the Jobs tab — the shortlist is what the button is for.
      setActiveTab("jobs");
    } catch (err) {
      setJobsError(
        err instanceof Error ? err.message : "We could not find jobs for this candidate just now.",
      );
      // Land on the Jobs tab even on a failure: the error is rendered there,
      // and the recruiter who clicked the button must see it rather than be
      // left on a tab that gives no sign anything happened.
      setActiveTab("jobs");
    } finally {
      setJobsLoading(false);
    }
  }

  // The shared state every intelligence stage reads to decide empty vs loading
  // vs failed. Derived once from the hook so the three stage panels render
  // consistently. Mirrors `stageState` in `detail-panel.tsx`.
  const stageState: CandidateStageState = {
    hasAnalysis: !!ci.analysis,
    waiting: ci.waiting,
    failed: ci.view !== null && "state" in ci.view && ci.view.state === "failed",
    failureReason:
      ci.view !== null && "failure_reason" in ci.view ? ci.view.failure_reason : null,
    loading: ci.phase.status === "loading",
    readError: ci.phase.status === "error" ? ci.phase.message : null,
  };

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
        <span className="cand-title-row">
          <span className="cand-title-text">
            {row.record_status === "merged"
              ? `${row.full_name} (merged)`
              : row.full_name}
          </span>
          {/* The action buttons live in the header, top-right, not at the
              bottom of the modal. A recruiter scanning a long record reaches
              for Save / Share / Archive from wherever they are scrolled to,
              and a footer bar is below a fold that has no affordance of its
              own. In the header they are always visible — the same place the
              job-orders modal keeps "Run analysis". The `!canEdit` note that
              used to sit beside the footer buttons moves to the body below,
              where the fields it explains also live. */}
          <span className="cand-title-actions">
            {row.record_status !== "merged" && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void runAnalysis()}
                disabled={ci.starting || ci.waiting}
                title="Run the candidate intelligence analysis"
              >
                {ci.starting
                  ? "Starting…"
                  : ci.waiting
                    ? "Analysing…"
                    : ci.analysis
                      ? "Re-run analysis"
                      : "Run analysis"}
              </button>
            )}
            {/* Find Job is a different kind of action from the analysis: it
                scores the job orders this recruiter can see against the
                candidate's profile, on demand — no run to poll, no stored
                record. Styled as the other action buttons in the row so it
                reads as one of them, not as a secondary control. */}
            {row.record_status !== "merged" && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void findJobs()}
                disabled={jobsLoading}
                title="Shortlist the job orders that best fit this candidate"
              >
                {jobsLoading ? "Finding…" : "Find Job"}
              </button>
            )}
            {row.record_status !== "merged" && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={saveFields}
                disabled={busy || !canEdit || savingFields}
                title={canEdit ? undefined : "Only the recruiter who holds this candidate can edit it."}
              >
                {savingFields ? "Saving…" : "Save changes"}
              </button>
            )}
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
          </span>
        </span>
      }
    >
      {/* The close affordance, pinned to the modal's upper-right corner — the
          same treatment as the job-order modal's `.jo-detail-close`. The
          backdrop click and Escape already close the modal, but neither is
          discoverable — a visible × is where a user looks first, and the one
          control always reachable from anywhere in the record. Red because
          closing is the destructive half of "I am done here". Disabled while
          a save is in flight, matching the Dialog's own guarded close. */}
      <button
        type="button"
        className="cand-detail-close"
        onClick={onClose}
        disabled={busy}
        aria-label="Close"
      >
        <span aria-hidden="true">×</span>
      </button>
      {row.record_status === "merged" ? (
        <MergedInto row={row} onUnmerge={unmerge} busy={busy} />
      ) : (
        <>
          {ci.runError && (
            <p className="body jo-detail-error" role="alert">
              {ci.runError}
            </p>
          )}
          {/* The tab bar mirrors the job-order modal's Origin/Work/Person/Search
              strip. Details is the existing editable record (default); the other
              three are the Candidate Intelligence stages. */}
          <div className="cand-tabs" role="tablist" aria-label="Candidate view">
            {CANDIDATE_TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                role="tab"
                aria-selected={activeTab === t.key}
                className={activeTab === t.key ? "cand-tab cand-tab-on" : "cand-tab"}
                onClick={() => setActiveTab(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>

          {/* One container for every tab's content. `minHeight` is the Details
              height (measured once it renders), locked so the modal stops
              collapsing and re-growing on every tab switch. Mirrors the
              `jo-tab-panel` wrapper in the job-orders modal. */}
          <div ref={panelRef} style={panelMinHeight ? { minHeight: panelMinHeight } : undefined}>
          {activeTab === "assessment" && (
            <AssessmentStage intelligence={ci.analysis} state={stageState} />
          )}
          {activeTab === "work" && (
            <WorkStage intelligence={ci.analysis} state={stageState} />
          )}
          {activeTab === "education" && (
            <EducationStage intelligence={ci.analysis} state={stageState} />
          )}
          {activeTab === "jobs" && (
            <JobsStage jobs={jobs} loading={jobsLoading} error={jobsError} onRun={findJobs} />
          )}

          {activeTab === "details" && (
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
            <SalaryGroup
              label="Last drawn salary"
              amount={form.last_drawn_salary}
              onAmount={(v) => set("last_drawn_salary", v)}
              currency={form.last_drawn_currency}
              onCurrency={(v) => set("last_drawn_currency", v)}
              period={form.last_drawn_period}
              onPeriod={(v) => set("last_drawn_period", v)}
              amountPlaceholder="2500"
              disabled={!canEdit || savingFields}
            />
            <SalaryGroup
              label="Expected salary"
              amount={form.expected_salary}
              onAmount={(v) => set("expected_salary", v)}
              currency={form.salary_currency}
              onCurrency={(v) => set("salary_currency", v)}
              period={form.salary_period}
              onPeriod={(v) => set("salary_period", v)}
              amountPlaceholder="2800"
              disabled={!canEdit || savingFields}
            />
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

            {/* The two regulatory fieldsets live INSIDE `.cand-form`, exactly as
                they do in the create form (`candidate-form.tsx`): their
                `cand-group-permit`/`cand-group-record` rules set `grid-column:
                span 2`/`span 1`, which is meant for this 2-column form grid.
                Moved out here as siblings of `.cand-form` (direct children of
                `.dlg-modal`), the `span 2` forced the modal's own grid to grow
                an implicit second column — which is what made the title sit on
                the left and every field pile onto the right. Inside the form
                grid, the span partitions a row the way it was designed to. */}
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
          </div>

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
          </div>
        </>
      )}

      {/* The "can't edit" note used to sit beside the footer buttons. With the
          buttons moved to the header, the note stays in the body next to the
          fields it explains — a reader who finds every input disabled reads
          why immediately, rather than having to scroll to a footer that no
          longer exists. */}
      {!canEdit && row.record_status !== "merged" && (
        <p className="body jo-sub">
          {unclaimed
            ? "Nobody holds this candidate yet. Claim them to edit the record — an unclaimed record is where a wrong edit is least likely to be noticed, so the server refuses it too."
            : `${ownerName ?? "A colleague"} holds this candidate. You can read the record, log a WhatsApp message against it, and pass it on to someone else — but only they can change it.`}
        </p>
      )}

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

/**
 * The Jobs tab: the "Find Job" shortlist.
 *
 * A candidate-specific sibling of the job-order modal's `Shortlist` section —
 * the same score and breakdown vocabulary, read in the other direction. Where
 * sourcing stores an async run, this is a synchronous read: the server scores
 * the visible current revisions against the candidate's profile and returns
 * the top few, best first. The order the server sent is the order to render —
 * never re-sorted here, since the scores arrive as strings from a NUMERIC
 * column.
 *
 * allow-hardcode: the copy below is recruiter-facing text, not a list anything
 * is matched against.
 */

/** What each scoring component is called in front of a recruiter. An unknown
 *  name falls back to itself, so a component added server-side shows up as
 *  something rather than vanishing from the breakdown. */
const JOB_COMPONENT_LABELS: Record<string, string> = {
  title: "Job title",
  semantic: "CV match",
  skills: "Skills",
  employer: "Employer",
  salary: "Salary",
  tenure: "Experience",
  recency: "Recent activity",
};

function JobsStage({
  jobs,
  loading,
  error,
  onRun,
}: {
  jobs: CandidateJobs | null;
  loading: boolean;
  error: string | null;
  onRun: () => void;
}) {
  // Kept apart from "not run yet", and never collapsed into it: a failed read
  // rendered as "run Find Job" is a claim about the feature rather than about
  // our server.
  if (error) {
    return (
      <div className="cand-jobs">
        <p className="body cand-jobs-error" role="alert">
          {error}
        </p>
        <button type="button" className="btn btn-secondary" onClick={onRun} disabled={loading}>
          Try again
        </button>
      </div>
    );
  }
  if (loading && !jobs) {
    return (
      <p className="body cand-jobs-note" aria-live="polite">
        Scoring the job orders you can see against this candidate&rsquo;s profile.
      </p>
    );
  }
  if (!jobs) {
    return (
      <p className="body cand-jobs-note">
        No shortlist yet. Use &ldquo;Find Job&rdquo; at the top to shortlist the job orders
        that best fit this candidate&rsquo;s profile.
      </p>
    );
  }
  if (jobs.items.length === 0) {
    return (
      <p className="body cand-jobs-note">
        Nothing scored high enough to be worth showing. {jobs.considered.toLocaleString()}{" "}
        visible job order{jobs.considered === 1 ? " was" : "s were"} examined — adding skills or
        recent roles to this candidate is what changes the answer.
      </p>
    );
  }
  return (
    <div className="cand-jobs">
      <p className="body cand-jobs-note">
        The best {jobs.items.length} of {jobs.scored.toLocaleString()} scoreable job order
        {jobs.scored === 1 ? "" : "s"} (of {jobs.considered.toLocaleString()} visible), ranked by
        how well each fits this candidate&rsquo;s profile. Scores are a weighted match across
        title, skills, employer, salary and experience.
      </p>
      <ol className="cand-jobs-list">
        {jobs.items.map((match, index) => (
          <li key={match.id} className="cand-jobs-row">
            <span className="cand-jobs-rank" aria-hidden="true">
              {index + 1}
            </span>
            <div className="cand-jobs-body">
              <div className="cand-jobs-head">
                <span className="cand-jobs-title">
                  {match.job_title_raw ?? "Untitled role"}
                  {match.company_name_raw && (
                    <span className="cand-jobs-company"> at {match.company_name_raw}</span>
                  )}
                </span>
                <span
                  className="cand-jobs-score"
                  title="Match score — how well this job order fits the candidate's profile."
                >
                  {percent(Number(match.score), match.score)}
                </span>
              </div>
              <JobFacts match={match} />
              {match.review_status === "needs_review" && (
                <p className="body cand-jobs-chip">Needs review</p>
              )}
              <JobBreakdown reasons={match.reasons} />
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** The raw facts a recruiter scans before the score: what the vacancy is, in
 *  the words the email carried. Each `_raw` string is shown as recorded —
 *  never normalised, because the recruiter recognises what the client wrote. */
function JobFacts({ match }: { match: CandidateJobMatch }) {
  const parts = [
    match.salary_raw,
    match.location_raw,
    match.duration_raw,
    match.working_hours_raw,
    match.employment_type,
  ].filter((p): p is string => Boolean(p));
  if (parts.length === 0) return null;
  return <p className="body cand-jobs-facts">{parts.join(" · ")}</p>;
}

/** Why this job order is where it is. The arithmetic behind the score, so
 *  "why is it third?" has an answer on the page rather than in a log. */
function JobBreakdown({ reasons }: { reasons: CandidateJobReason[] | null }) {
  if (!reasons || reasons.length === 0) return null;

  return (
    <ul className="cand-jobs-reasons">
      {reasons.map((reason) => (
        <li key={reason.name} className="cand-jobs-reason">
          <span className="cand-jobs-reason-k">
            {JOB_COMPONENT_LABELS[reason.name] ?? reason.name}
          </span>
          {/* Null means nothing was recorded to compare, which is not the same
              as scoring zero — saying "0%" here would put "a poor fit on
              salary" in front of a recruiter when the truth is that nobody
              stated one. A scored component reads as the percentage of its
              weight the job order earned. */}
          <span
            className={reason.contribution === null ? "cand-jobs-reason-v muted" : "cand-jobs-reason-v"}
            title={
              reason.contribution === null
                ? undefined
                : "The share of this criterion's possible weight the job order earned."
            }
          >
            {reason.contribution === null
              ? (reason.note ?? "Nothing to compare")
              : percent(
                  Number(reason.contribution) / Number(reason.weight),
                  `${reason.contribution} of ${reason.weight}`,
                )}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** A 0–1 fraction as a whole percentage, for display only. The stored numbers
 *  are strings (a NUMERIC column; a float round-trip would show
 *  0.6499999999999999 for a value the scorer computed exactly), and are
 *  parsed here purely to render — never to compare or re-order, which is the
 *  server's job. Falls back to `fallback` when the fraction is not finite. */
function percent(fraction: number, fallback: string): string {
  return Number.isFinite(fraction) ? `${Math.round(fraction * 100)}%` : fallback;
}
