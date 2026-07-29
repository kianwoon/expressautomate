"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { CANDIDATE_IMPORT_TEMPLATE_PATH } from "../../api";
import {
  getCandidateImportErrorsUrl,
  listCandidateImports,
  undoCandidateImport,
  uploadCandidateImport,
} from "../candidates";
import type { CandidateImport, ImportSheet, UndoResult } from "../candidates";

/**
 * Where an agency watches its list arrive, and can take it back.
 *
 * On the candidates page rather than in the detail panel: an import is a bulk
 * act on the whole list, and half of what it does is create people who by
 * definition have no panel open on them yet.
 *
 * The upload keeps the bargain `candidate-avatar.tsx` and `candidate-cv.tsx`
 * struck — the drop panel *is* the control, with a real file input behind it
 * rather than a click target synthesised from a div, so the whole thing is
 * operable from the keyboard and lights up from `:focus-within` as readily as
 * from a hover.
 *
 * The sheet picker is the one thing the panel cannot infer. A CSV is a single
 * nameless sheet, so somebody has to say whether it holds people or the jobs
 * they held; the server decides from that field and never from the filename,
 * and the picker therefore stays on screen for an XLSX too — which carries both
 * sheets by name and ignores the answer — rather than appearing and vanishing
 * on an extension nothing trusts.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not values anything is matched against.
 */

/** Only while something is genuinely being read. A page whose imports have all
 *  finished asks nothing of the server at all. Matches the CV panel's cadence,
 *  for the same reason: fast enough to feel live, slow enough that a five
 *  hundred row sheet is not answered by five hundred requests. */
const POLL_MS = 6000;

/** A hint to the file picker, not a check. The server sniffs the bytes and
 *  refuses with a 415 that says so, which is the only judgement that counts —
 *  a `.csv` full of PDF is still a PDF. */
const ACCEPT = ".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

const SHEETS: { value: ImportSheet; label: string }[] = [
  { value: "Candidates", label: "Candidates — one row per person" },
  { value: "History", label: "History — one row per job they held" },
];

/** The two states a job is still moving through. Everything else is terminal,
 *  and polling one would be asking the same question forever. */
function inFlight(row: CandidateImport): boolean {
  return row.state === "pending" || row.state === "parsing";
}

/** What the state means, said once, in words rather than in a status code.
 *
 * `failed` and a `done` run with failed rows are deliberately not phrased
 * alike: the first is the whole file falling over, the second is the ordinary
 * case of a sheet that mostly landed. Reading them as the same thing sends
 * someone to re-upload a file that already worked. */
function stateLine(row: CandidateImport): string {
  switch (row.state) {
    case "pending":
      return "Queued. Nothing has been read yet.";
    case "parsing":
      return "Reading the rows now.";
    case "failed":
      return "This run did not finish. Nothing from it was kept.";
    case "undone":
      return "Reversed.";
    default:
      return row.rows_failed > 0
        ? "Finished, with some rows we could not read."
        : "Finished.";
  }
}

const STATE_LABEL: Record<CandidateImport["state"], string> = {
  pending: "Queued",
  parsing: "Reading",
  done: "Done",
  failed: "Failed",
  undone: "Undone",
};

/** The counts as a sentence, and silent when there is nothing to count.
 *  "0 created, 0 updated, 0 roles" on a queued import is three facts about a
 *  file nobody has opened yet. */
function countLine(row: CandidateImport): string | null {
  const parts: string[] = [];
  if (row.candidates_created) parts.push(`${row.candidates_created} added`);
  if (row.candidates_updated) parts.push(`${row.candidates_updated} updated`);
  if (row.roles_created) parts.push(`${row.roles_created} roles added`);
  if (row.roles_updated) parts.push(`${row.roles_updated} roles updated`);
  if (row.rows_failed) parts.push(`${row.rows_failed} rows failed`);
  return parts.length ? parts.join(", ") : null;
}

function when(iso: string | null): string {
  if (!iso) return "";
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? "" : at.toLocaleString();
}

/**
 * What the undo actually did, in the recruiter's words.
 *
 * The `already_undone` branch is the important one. The server short-circuits a
 * second undo rather than running it, precisely because a re-run would find
 * every created row already gone and every field already restored and report
 * that as a page of skips — its own first pass's work, dressed up as a
 * recruiter's edits. Saying "we protected 40 of your changes" there would be a
 * lie in the one place a person is checking whether their data is safe, so a
 * repeat undo reads as what it is: there is nothing left to reverse.
 */
function undoLine(result: UndoResult): string {
  if (result.already_undone) return "Nothing left to reverse — this import was already undone.";
  const deleted = result.rows_deleted ?? 0;
  const restored = result.fields_restored ?? 0;
  if (!deleted && !restored) return "Nothing left to reverse.";
  const parts: string[] = [];
  if (deleted) parts.push(`deleted ${deleted} ${deleted === 1 ? "row" : "rows"} it created`);
  if (restored) parts.push(`put back ${restored} ${restored === 1 ? "field" : "fields"} it changed`);
  return `Reversed: ${parts.join(", ")}.`;
}

export function CandidateImports({
  onImported,
  onUndone,
}: {
  /** Re-read the candidate list. Called on every poll while a run is moving,
   *  because the point of watching an import is that the table beside it
   *  fills up. */
  onImported: () => void;
  /** The same, plus whatever the caller must let go of. An undo deletes
   *  people, and one of them may be the person whose panel is open — a
   *  selection kept across that becomes a detail fetch for a row that is no
   *  longer there. Separate from `onImported` because clearing the selection on
   *  every poll would be a panel that refuses to stay open. */
  onUndone: () => void;
}) {
  const [rows, setRows] = useState<CandidateImport[] | null>(null);
  const [sheet, setSheet] = useState<ImportSheet>("Candidates");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<{ id: string; result: UndoResult } | null>(null);
  const [reclaimFocus, setReclaimFocus] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const refetch = useCallback(async () => {
    try {
      setRows(await listCandidateImports());
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not read your recent imports.");
    }
  }, []);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  const waiting = (rows ?? []).some(inFlight);

  // `onImported` re-reads the candidate list, which is the point of polling: a
  // migration is worth watching because the table beside it fills up. Held in a
  // ref rather than named as a dependency, so a fresh closure from the parent
  // does not tear down and rebuild the interval on every tick — which is a way
  // of never quite firing it.
  const finished = useRef(onImported);
  finished.current = onImported;
  const undone = useRef(onUndone);
  undone.current = onUndone;

  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => {
      void refetch();
      finished.current();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [waiting, refetch]);

  // Focus, deferred until the input can actually take it.
  //
  // Undo removes the button that was pressed — an undone import has nothing
  // left to undo — and left alone focus falls to the body, so a keyboard
  // restarts at the top of the document. The upload input is the one control
  // on this section that is always here. Focusing it inside the handler is what
  // has bitten this codebase before: the input is still `disabled` while the
  // request is in flight, and a disabled element silently refuses focus. So the
  // intent is recorded and spent here, once `busy` has cleared and the input is
  // real again.
  useEffect(() => {
    if (!reclaimFocus || busy) return;
    fileRef.current?.focus();
    setReclaimFocus(false);
  }, [reclaimFocus, busy]);

  const upload = useCallback(
    async (file: File) => {
      if (busy) return;
      setBusy(true);
      setError(null);
      setOutcome(null);
      try {
        await uploadCandidateImport(file, sheet);
        await refetch();
        // A 202 has written nothing yet, but the poll below only starts once
        // the row is on screen, so this is the tick that says "queued".
        finished.current();
      } catch (err) {
        setError(err instanceof Error ? err.message : "We could not upload that sheet just now.");
      } finally {
        setBusy(false);
      }
    },
    [busy, refetch, sheet],
  );

  function onFileChosen(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // so choosing the same file twice fires onChange again
    if (file) void upload(file);
  }

  function onDrop(event: React.DragEvent) {
    event.preventDefault();
    setDragging(false);
    // A drag can carry a folder, a row of text, anything at all. Only a file is
    // worth sending; what kind of file it is, the server decides by reading the
    // bytes rather than by trusting anything said about them here.
    const file = event.dataTransfer.files?.[0];
    if (file) void upload(file);
  }

  async function runUndo(row: CandidateImport) {
    setBusy(true);
    setError(null);
    setConfirming(null);
    try {
      const result = await undoCandidateImport(row.id);
      setOutcome({ id: row.id, result });
      await refetch();
      // The list itself changed under us — undo deletes people, possibly the
      // one whose panel is open.
      undone.current();
    } catch (err) {
      // A 409 here is the run still parsing, and the server's sentence says to
      // wait. It is more useful than anything we could write in its place.
      setError(err instanceof Error ? err.message : "We could not undo that import just now.");
    } finally {
      setBusy(false);
      setReclaimFocus(true);
    }
  }

  return (
    <section className="ci-section" aria-label="Import a spreadsheet">
      <div className="ci-head">
        <span className="row-k">Import a spreadsheet</span>
        {/* A plain link, not a fetch: the template is a file, and the browser
            already knows how to save one. Without it an agency is guessing our
            column names, and a guessed column is a column silently dropped. */}
        <a className="ci-template" href={CANDIDATE_IMPORT_TEMPLATE_PATH} download>
          Download the template
        </a>
      </div>

      <div
        className="ci-drop"
        data-drop={dragging ? "yes" : undefined}
        data-busy={busy ? "yes" : undefined}
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        {/* Outside the label on purpose. Inside it, choosing a sheet would also
            open the file picker, and the two decisions are made in that order. */}
        <label className="ci-sheet">
          <span className="ci-sheet-k">This file is</span>
          <select
            className="ci-sheet-v"
            value={sheet}
            disabled={busy}
            onChange={(event) => setSheet(event.target.value as ImportSheet)}
          >
            {SHEETS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <label className="ci-trigger">
          <span className="ci-invite">
            {busy ? "Working…" : "Drop a spreadsheet here, or click to choose one."}
          </span>
          <span className="ci-kinds">
            CSV or Excel. An Excel workbook carries both sheets, so the choice above is only read
            for a CSV.
          </span>
          {/* Visually hidden but not `display: none`: it stays in the tab order,
              so the panel is reachable without a mouse, and lights up from
              `:focus-within`. */}
          <input
            className="ca-file"
            ref={fileRef}
            type="file"
            accept={ACCEPT}
            onChange={onFileChosen}
            disabled={busy}
            aria-label="Upload a candidate spreadsheet"
          />
        </label>
      </div>

      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}

      {rows === null ? (
        <p className="body muted ci-empty">Loading your recent imports…</p>
      ) : rows.length === 0 ? (
        <p className="body muted ci-empty">Nothing has been imported yet.</p>
      ) : (
        <ul className="ci-list" aria-live="polite">
          {rows.map((row) => (
            <ImportRow
              key={row.id}
              row={row}
              busy={busy}
              confirming={confirming === row.id}
              outcome={outcome?.id === row.id ? outcome.result : null}
              onAskUndo={() => {
                setOutcome(null);
                setConfirming(row.id);
              }}
              onCancelUndo={() => {
                setConfirming(null);
                setReclaimFocus(true);
              }}
              onUndo={() => void runUndo(row)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function ImportRow({
  row,
  busy,
  confirming,
  outcome,
  onAskUndo,
  onCancelUndo,
  onUndo,
}: {
  row: CandidateImport;
  busy: boolean;
  confirming: boolean;
  outcome: UndoResult | null;
  onAskUndo: () => void;
  onCancelUndo: () => void;
  onUndo: () => void;
}) {
  const counts = countLine(row);
  // Offered only once the import has settled. `pending` is not settled: the
  // job can claim it at any moment, so an undo from here would race the run
  // and could report a reversal the job then wrote straight over. The server
  // refuses it either way; the button should not invite it.
  //
  // Refused while parsing, and pointless once undone — the server says both,
  // but offering a button that can only be told off is its own small lie.
  const canUndo = row.state === "done" || row.state === "failed";

  return (
    <li className="ci-row" data-state={row.state}>
      <div className="ci-row-main">
        <span className="ci-file">{row.filename}</span>
        <span className="ci-when">{when(row.created_at)}</span>
      </div>

      <div className="ci-row-state">
        <span className="ci-badge" data-state={row.state}>
          {STATE_LABEL[row.state]}
        </span>
        <span className="ci-state-line">{stateLine(row)}</span>
        {counts && <span className="ci-counts">{counts}</span>}
        {inFlight(row) && <span className="ci-pulse" aria-hidden="true" />}
      </div>

      <div className="ci-row-acts">
        {row.has_errors && <ErrorReportLink importId={row.id} />}
        {canUndo &&
          (confirming ? (
            <UndoConfirm row={row} busy={busy} onUndo={onUndo} onCancel={onCancelUndo} />
          ) : (
            <button type="button" className="ci-act" disabled={busy} onClick={onAskUndo}>
              Undo
            </button>
          ))}
      </div>

      {outcome && (
        <div className="ci-outcome" role="status">
          <p className="body">{undoLine(outcome)}</p>
          {outcome.skips && outcome.skips.length > 0 && (
            <>
              {/* Deliberately not "we protected your changes". Some of these are
                  a later edit we kept, and some are a row that had since gained
                  work of its own — the reason the server wrote is the only
                  honest label, so it is shown rather than summarised. */}
              <p className="body muted ci-skip-lead">
                {outcome.skips.length === 1
                  ? "One thing was left as it is:"
                  : `${outcome.skips.length} things were left as they are:`}
              </p>
              <ul className="ci-skips">
                {outcome.skips.map((skip) => (
                  <li key={`${skip.entity_id}-${skip.field_name}`} className="body muted">
                    {skip.reason}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * What undo will reverse, in counts, before it runs.
 *
 * Undo deletes the candidates this import created. It protects fields a
 * recruiter has since corrected, and it keeps any created candidate somebody
 * has since worked on at all — a role, an edited field, an uploaded CV, a
 * skill, a move through the pipeline — but a plain created row with no later
 * work on it simply goes. That is worth a sentence and a second click, not a
 * silent button.
 *
 * The sentence below names the kinds rather than saying "later work", because
 * the reader is about to decide whether their own afternoon is safe and only
 * they know which kind of work they did.
 *
 * The counts are the import's own, so this is honestly phrased as the most it
 * will do rather than as a promise: the protections above can only ever make
 * the real number smaller, and the result afterwards says which of them fired.
 */
function UndoConfirm({
  row,
  busy,
  onUndo,
  onCancel,
}: {
  row: CandidateImport;
  busy: boolean;
  onUndo: () => void;
  onCancel: () => void;
}) {
  const deletes = row.candidates_created;
  const restores = row.candidates_updated;
  const parts: string[] = [];
  if (deletes) {
    parts.push(
      `delete up to ${deletes} ${deletes === 1 ? "candidate" : "candidates"} this import added`,
    );
  }
  if (restores) {
    parts.push(
      `put back the fields it changed on ${restores} ${restores === 1 ? "candidate" : "candidates"}`,
    );
  }
  const sentence = parts.length
    ? `This will ${parts.join(", and ")}.`
    : "This import wrote nothing, so there is nothing to reverse.";

  return (
    <div className="ci-confirm">
      <p className="body ci-confirm-line">
        {sentence}
        {deletes > 0 && (
          <>
            {" "}
            Anyone somebody has worked on since — a role or skill added, a field edited, a CV
            uploaded, a move to another stage — is kept, and a field somebody has corrected is
            left alone.
          </>
        )}
      </p>
      <div className="ci-confirm-acts">
        <button type="button" className="ci-act ci-act-danger" disabled={busy} onClick={onUndo}>
          Undo this import
        </button>
        <button type="button" className="ci-act" disabled={busy} onClick={onCancel}>
          Keep it
        </button>
      </div>
    </div>
  );
}

/**
 * The row-by-row report, fetched at the moment it is asked for.
 *
 * A button rather than an anchor, because there is no URL to put in an `href`
 * until somebody asks: the link is signed per request and expires within
 * minutes, so a URL rendered into the page at load would be a dead link by the
 * time it was clicked — and, worse, a live capability sitting in the DOM for
 * anyone who read it.
 */
function ErrorReportLink({ importId }: { importId: string }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  async function open() {
    if (busy) return;
    setBusy(true);
    setFailed(null);
    try {
      const { url } = await getCandidateImportErrorsUrl(importId);
      window.location.assign(url);
    } catch (err) {
      setFailed(err instanceof Error ? err.message : "We could not fetch that report just now.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button type="button" className="ci-act" disabled={busy} onClick={() => void open()}>
        {busy ? "Fetching…" : "What failed"}
      </button>
      {failed && (
        <span className="body jo-detail-error" role="alert">
          {failed}
        </span>
      )}
    </>
  );
}
