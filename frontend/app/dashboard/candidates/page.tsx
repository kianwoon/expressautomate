"use client";

import { useCallback, useEffect, useState } from "react";

import { CANDIDATES_PAGE_SIZE, DASHBOARD_PATH, LANDING_PATH } from "../../api";
import { useAuth } from "../../auth";
import { SiteFooter } from "../../site-footer";
import { SiteNav } from "../../site-nav";
import {
  archiveCandidate,
  deleteCandidate,
  getCandidate,
  restoreCandidate,
  useCandidates,
} from "../candidates";
import type { Candidate, Filter, ListState, Scope } from "../candidates";
import { getOpportunity } from "../opportunities";
import type { Opportunity } from "../opportunities";
import { CandidateForm } from "./candidate-form";
import { CandidateImports } from "./candidate-imports";
import { CandidatePanel } from "./candidate-panel";
import { AccessRequestInbox } from "./candidate-share";
import { CandidatesTable } from "./candidates-table";

/**
 * The signed-in screen for the people an agency places, mirroring how
 * `dashboard/page.tsx` composes the job-orders workspace: the same
 * `useAuth()` and the same anonymous-user redirect, so there is only one
 * auth pattern in the codebase rather than two.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the
 * page, not a list anything is matched against.
 */

const CHIPS: { key: Filter; label: string }[] = [
  { key: null, label: "All" },
  { key: "new", label: "New" },
  { key: "contacted", label: "Contacted" },
  { key: "submitted", label: "Submitted" },
  { key: "placed", label: "Placed" },
  { key: "rejected", label: "Rejected" },
  // Its own chip, and load-bearing: merged rows are hidden from the default
  // list and the pointer runs loser -> survivor, so this is the only route
  // back to a wrongly merged person.
  { key: "merged", label: "Merged" },
];

/**
 * Whose candidates, which is a different question from what stage they are at.
 *
 * The same four, in the same order, with the same words as the job order list
 * — `SCOPES` in `job-orders.tsx`. Copied deliberately rather than generalised:
 * a shared component would have to carry both pages' chip rows, and the thing
 * that matters here is that the two screens read alike, which a recruiter
 * checks with their eyes and not with an import.
 *
 * Its own row under the stage chips, again as job orders has it. On one line
 * the two would read as a single list of alternatives, which is the one thing
 * they are not: "mine, at the submitted stage" is one question.
 *
 * No counts. The endpoint counts per pipeline stage, not per scope, and a
 * number worked out in the browser would count the page rather than the set.
 */
const SCOPES: { key: Scope; label: string }[] = [
  { key: "mine", label: "Mine" },
  { key: "queue", label: "Queue" },
  { key: "shared_with_me", label: "Shared with me" },
  { key: "all", label: "Everyone" },
];

/** Everything the bar can ever offer, in the order it is always drawn. `#` is
 *  the server's own name for a name that does not start with an A–Z letter —
 *  a Chinese or Tamil name, or one recorded with a leading digit. */
const NON_ALPHA_INITIAL = "#";
const INITIALS: string[] = [
  ...Array.from({ length: 26 }, (_, i) => String.fromCharCode(65 + i)),
  NON_ALPHA_INITIAL,
];

export default function CandidatesPage() {
  const auth = useAuth();

  useEffect(() => {
    if (auth.status === "anonymous") window.location.replace(LANDING_PATH);
  }, [auth.status]);

  return (
    <>
      <SiteNav />
      <main>
        <section className="hero" style={{ paddingBottom: 48 }}>
          <div className="wrap" aria-live="polite">
            {auth.status === "signed-in" ? (
              <Workspace role={auth.me.user.role} />
            ) : auth.status === "unreachable" ? (
              <Notice
                heading="We could not reach the server."
                body="This is not a sign-in problem — your session is untouched. Reload the page in a moment."
              />
            ) : auth.status === "anonymous" ? (
              <Notice heading="Taking you back." body="You are not signed in. Returning to the home page." />
            ) : (
              <Notice heading="One moment." body="Checking your session." />
            )}
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}

/** The pre-workspace states. No eyebrow, and the same top spacing as the
 *  workspace it gives way to: "CONNECTION" over "We could not reach the
 *  server." said in a label what the sentence under it says in words, and it
 *  meant the heading sat 14px lower here than in the page that replaces it —
 *  so the whole page nudged upward the moment the session resolved. */
function Notice({ heading, body }: { heading: string; body: string }) {
  return (
    <>
      <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>{heading}</h1>
      <p className="lede" style={{ marginTop: 18 }}>
        {body}
      </p>
    </>
  );
}

type View = { mode: "list" } | { mode: "create" } | { mode: "edit"; row: Candidate };

// Read once, on module init of the component — the URL is how the job order
// handed this page its context, and re-reading it on every render would let
// a `setOffset` or a chip click that leaves `?eligible_for=…` sitting in the
// address bar (nobody rewrites it except `clearEligibleFor` below) silently
// re-apply a filter the recruiter thought they had cleared.
function initialEligibleFor(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("eligible_for");
}

function Workspace({ role }: { role: string }) {
  const {
    state,
    filter,
    offset,
    q,
    initial,
    eligibleFor,
    scope,
    counts,
    initials,
    refreshing,
    setFilter,
    setOffset,
    setQ,
    setInitial,
    setEligibleFor,
    setScope,
    reload,
  } = useCandidates(initialEligibleFor());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Candidate | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [view, setView] = useState<View>({ mode: "list" });

  // The candidates endpoint has no reason to know a job order's title — the
  // banner does, so it is fetched separately, the same split `detail` above
  // keeps between the list and the one record it is about. `undefined` is
  // "still loading", `null` is "could not read it" (e.g. the job order was
  // deleted), and both render a banner that at least names the filter as
  // active rather than waiting on a title before saying anything.
  const [jobOrder, setJobOrder] = useState<Opportunity | null | undefined>(
    eligibleFor ? undefined : null,
  );
  useEffect(() => {
    if (!eligibleFor) {
      setJobOrder(null);
      return;
    }
    let cancelled = false;
    setJobOrder(undefined);
    getOpportunity(eligibleFor)
      .then((row) => {
        if (!cancelled) setJobOrder(row);
      })
      .catch(() => {
        if (!cancelled) setJobOrder(null);
      });
    return () => {
      cancelled = true;
    };
  }, [eligibleFor]);

  // Returns to the ordinary list and takes `eligible_for` out of the address
  // bar with it — a filter that clears in the list but stays in the URL
  // would silently reapply itself on the next reload.
  const clearEligibleFor = useCallback(() => {
    setEligibleFor(null);
    setSelectedId(null);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.delete("eligible_for");
      window.history.replaceState(null, "", url.toString());
    }
  }, [setEligibleFor]);

  const items = state.status === "ready" ? state.page.items : EMPTY;
  const total = state.status === "ready" ? state.page.total : 0;
  const limit = state.status === "ready" ? state.page.limit : CANDIDATES_PAGE_SIZE;

  // Open on the first row once the page loads, the same fallback job orders
  // uses: a panel that starts blank spends the first screenful asking to be
  // clicked.
  useEffect(() => {
    if (!selectedId && items.length > 0) setSelectedId(items[0].id);
  }, [items, selectedId]);

  // The panel needs the full record — skills and `overridden_fields` are only
  // on the single-record GET, never on a list row — so it is fetched
  // separately whenever the selection changes.
  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailError(null);
    (async () => {
      try {
        const full = await getCandidate(selectedId);
        if (!cancelled) setDetail(full);
      } catch {
        if (!cancelled) setDetailError("We could not load that candidate just now.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  // Re-reads the one open record and nothing else. The avatar is deliberately
  // not drawn in the table, so a changed `avatar_key` cannot alter a visible
  // row and there is nothing for the list to learn from refetching it.
  const refetchDetail = useCallback(() => {
    if (!selectedId) return;
    getCandidate(selectedId)
      .then(setDetail)
      .catch(() => setDetailError("We could not load that candidate just now."));
  }, [selectedId]);

  // Archive, restore and merge all move a row between filters or out of the
  // list entirely, so those genuinely do need the list re-read as well.
  const refreshDetail = useCallback(() => {
    reload();
    refetchDetail();
  }, [reload, refetchDetail]);

  async function doArchive() {
    if (!detail) return;
    await archiveCandidate(detail.id);
    refreshDetail();
  }

  async function doRestore() {
    if (!detail) return;
    await restoreCandidate(detail.id);
    refreshDetail();
  }

  // Clears the selection along with the letter, exactly as paging does: the
  // open panel is almost certainly showing someone the new letter does not
  // contain, and leaving it there reads as a row that refuses to deselect.
  const pickInitial = useCallback(
    (next: string | null) => {
      setInitial(next);
      setSelectedId(null);
    },
    [setInitial],
  );

  // Lets go of the selection along with the scope, exactly as picking a letter
  // does: "Mine" almost certainly does not contain the colleague's candidate
  // the panel is open on, and leaving it there reads as a row that refuses to
  // deselect.
  const pickScope = useCallback(
    (next: Scope) => {
      setScope(next);
      setSelectedId(null);
    },
    [setScope],
  );

  // An undone import may have deleted the person the panel is open on. Re-read
  // the list and let go of the selection, rather than leaving a detail fetch
  // pointed at a row that is no longer there — which reads as "we could not
  // load that candidate" when the truth is that it is gone on purpose.
  const dropSelection = useCallback(() => {
    setSelectedId(null);
    setDetail(null);
    reload();
  }, [reload]);

  const canDelete = role === "owner";
  async function doDelete() {
    if (!detail) return;
    await deleteCandidate(detail.id);
    setSelectedId(null);
    setDetail(null);
    reload();
  }

  // Rendered *over* the list rather than instead of it. The early return this
  // replaced sent the recruiter to a bare page: the row they were editing, the
  // filters they had set and the letter they had picked all vanished, and
  // cancelling had to rebuild the screen from state rather than just closing.
  const formDialog =
    view.mode === "list" ? null : (
      <CandidateForm
        row={view.mode === "edit" ? view.row : null}
        onCancel={() => setView({ mode: "list" })}
        onDone={(saved) => {
          setView({ mode: "list" });
          // Editing the row the panel is already open on leaves `selectedId`
          // unchanged, so the detail effect never re-runs and the panel keeps
          // showing the values from before the edit. The PATCH response is the
          // full record — the same shape the detail GET returns — so adopt it
          // directly rather than relying on the selection to change.
          setSelectedId(saved.id);
          setDetail(saved);
          setDetailError(null);
          reload();
        }}
      />
    );

  return (
    <>
      {formDialog}
      {/* No eyebrow above the heading: "CANDIDATES" over "The people you
          place." labelled the page twice, and the nav already says which page
          this is. The heading starts the page. */}
      <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>
        The people you place.
      </h1>
      <p className="lede" style={{ marginTop: 18 }}>
        Every candidate your agency has recorded, tenant-wide — nothing here came from an email;
        it was typed by a person or came from a sheet a person uploaded.
      </p>

      {eligibleFor && (
        <EligibleForBanner
          jobOrder={jobOrder}
          state={state}
          narrowed={filter !== null || q.trim() !== "" || initial !== null}
          onClear={clearEligibleFor}
        />
      )}

      <div className="jo-head" style={{ marginTop: 24 }}>
        <button type="button" className="btn btn-primary" onClick={() => setView({ mode: "create" })}>
          Add candidate
        </button>
      </div>

      {/* Above the filters rather than inside the table: an import acts on the
          whole list, and most of what it does is create people no filter is
          currently pointed at. */}
      {/* Above the imports and the filters both: somebody is waiting on an
          answer from this reader, and a request buried under the controls is
          a request that stays pending while they wait and then ask again.
          Draws nothing at all when the inbox is empty. */}
      <AccessRequestInbox onResolved={reload} />

      <CandidateImports onImported={reload} onUndone={dropSelection} />

      <div className="jo-controls" style={{ marginTop: 20 }}>
        <div className="jo-chips" role="group" aria-label="Filter candidates">
          {CHIPS.map((chip) => {
            const active = filter === chip.key;
            // No number at all while the eligibility filter is on. The chip
            // still filters — stage and eligibility compose fine on the
            // server — but the server counted the unfiltered population, so
            // the figure would sit above rows it does not describe.
            const n =
              counts === null || chip.key === "merged"
                ? null
                : chip.key === null
                  ? counts.all
                  : counts[chip.key];
            return (
              <button
                key={chip.label}
                type="button"
                className="jo-chip"
                data-active={active ? "yes" : undefined}
                aria-pressed={active}
                onClick={() => setFilter(chip.key)}
              >
                <span>{chip.label}</span>
                {/* The list endpoint's counts are grouped over non-merged rows
                    only, so there is no tenant-wide "merged" count to show
                    here without a second request — the chip stays a plain
                    link to `record_status=merged` rather than guessing a
                    number that would drift from what the filtered page
                    actually shows. */}
                {n != null && <span className="jo-chip-n">{n.toLocaleString()}</span>}
              </button>
            );
          })}
        </div>

        <input
          className="jo-search"
          type="search"
          value={q}
          onChange={(event) => setQ(event.target.value)}
          placeholder="Search name, email or phone…"
          aria-label="Search candidates"
        />
      </div>

      {/* Its own row, under the stage chips rather than beside them — see the
          note on `SCOPES`. This row is what makes "Everyone" honest: it was
          always "everyone I am allowed to see", and until these three sat
          beside it there was nothing on the screen saying so. */}
      <div className="jo-scopes">
        <div className="jo-chips" role="group" aria-label="Filter candidates by owner">
          {SCOPES.map((chip) => {
            const active = scope === chip.key;
            return (
              <button
                key={chip.key}
                type="button"
                className="jo-chip"
                data-active={active ? "yes" : undefined}
                aria-pressed={active}
                onClick={() => pickScope(chip.key)}
              >
                <span>{chip.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Always the whole alphabet, never only the letters in use: a bar whose
          buttons appear and disappear as the filters change moves the letter
          you were aiming at out from under the pointer. Absent letters are
          disabled instead — still in place, visibly not worth clicking. */}
      <nav className="jo-index" aria-label="Jump to candidates by first letter">
        <button
          type="button"
          className="jo-index-key"
          data-active={initial === null ? "yes" : undefined}
          aria-pressed={initial === null}
          onClick={() => pickInitial(null)}
        >
          All
        </button>
        {INITIALS.map((letter) => {
          const active = initial === letter;
          return (
            <button
              key={letter}
              type="button"
              className="jo-index-key"
              data-active={active ? "yes" : undefined}
              aria-pressed={active}
              // `null` means the server did not say which letters are
              // populated, which is not the same as saying none are. Leave
              // every letter live rather than greying out letters that may
              // well have eligible people behind them.
              disabled={initials !== null && !initials.includes(letter)}
              onClick={() => pickInitial(letter)}
            >
              {letter}
            </button>
          );
        })}
      </nav>

      {state.status === "loading" ? (
        <p className="body jo-note">Loading your candidates.</p>
      ) : state.status === "unreadable" ? (
        <p className="body jo-note" role="alert">
          {state.message}
        </p>
      ) : state.status === "blocked" ? (
        // The banner above already explains the 409 and offers a way out;
        // showing the ordinary empty-list line underneath it here would read
        // as a second, contradictory answer to the same question.
        <p className="body jo-note" role="alert">
          Set the placement type on that job order, then try again.
        </p>
      ) : items.length === 0 ? (
        <p className="body jo-note" aria-live="polite">
          {emptyLine(filter, initial, scope)}
        </p>
      ) : (
        <>
          <p className="body jo-note" aria-live="polite">
            Showing {offset + 1}–{offset + items.length} of {total.toLocaleString()}.
          </p>
          <div className="jo-split" aria-busy={refreshing || undefined}>
            <CandidatesTable rows={items} selectedId={selectedId} onSelect={setSelectedId} />
            <CandidatePanel
              row={detailError ? null : detail}
              onEdit={() => detail && setView({ mode: "edit", row: detail })}
              onArchive={doArchive}
              onRestore={doRestore}
              onDelete={canDelete ? doDelete : null}
              onChanged={refreshDetail}
              onDetailChanged={refetchDetail}
            />
          </div>
          {detailError && (
            <p className="body jo-detail-error" role="alert">
              {detailError}
            </p>
          )}
          <Pager
            total={total}
            limit={limit}
            offset={offset}
            onOffset={(next) => {
              setOffset(next);
              setSelectedId(null);
            }}
          />
        </>
      )}
    </>
  );
}

/**
 * The banner while `eligible_for` is on the URL — the one thing standing
 * between a filtered list and a list that quietly lost people. Three jobs:
 * say the filter is active and name the job order, report how many rows it
 * removed (qualified against `scanned` when the scan was truncated, never
 * against the agency's whole candidate count), and say plainly when
 * `scan_truncated` means "from the first N examined" rather than "everyone".
 */
function EligibleForBanner({
  jobOrder,
  state,
  narrowed,
  onClear,
}: {
  jobOrder: Opportunity | null | undefined;
  state: ListState;
  /** Whether a stage chip, a search or a letter is also active — every one of
   *  which narrows `base` before the scan ceiling applies. When it bites, the
   *  window is the first N of *that* set, not of the whole agency, and saying
   *  which is the difference between a caveat and a misleading one.
   *
   *  Deliberately says nothing about the sort: the order flips to alphabetical
   *  once a letter is picked, so naming one ordering is wrong on the other
   *  branch. "In the order shown here" is true either way, and the list is
   *  right there to be looked at. */
  narrowed: boolean;
  onClear: () => void;
}) {
  const roleLine =
    jobOrder === undefined
      ? "this role"
      : jobOrder === null
        ? "this role"
        : jobOrder.job_title_raw
          ? jobOrder.company_name_raw
            ? `${jobOrder.job_title_raw} at ${jobOrder.company_name_raw}`
            : jobOrder.job_title_raw
          : "this role";

  return (
    <div className="card cand-elig-banner" role="status">
      <div className="cand-elig-head">
        <span>
          Showing only candidates who can legally take <strong>{roleLine}</strong>.
        </span>
        <button type="button" className="btn btn-secondary" onClick={onClear}>
          Clear filter
        </button>
      </div>

      {state.status === "blocked" && (
        <p className="body jo-detail-error" role="alert">
          {state.detail || "This job order needs a placement type before it can be used this way."}{" "}
          <a href={DASHBOARD_PATH}>Set the placement type on the job order.</a>
        </p>
      )}

      {state.status === "ready" &&
        typeof state.page.excluded_ineligible === "number" &&
        (state.page.scan_truncated ? (
          <p className="body jo-sub cand-elig-note" role="alert">
            Not everyone was checked. We worked down{" "}
            {narrowed ? "the candidates matching your current filters" : "your candidates"} in the
            order shown here, stopped after {(state.page.scanned ?? 0).toLocaleString()}, and did
            not look further — eligible people may exist beyond that point.{" "}
            {state.page.excluded_ineligible.toLocaleString()} of those{" "}
            {(state.page.scanned ?? 0).toLocaleString()} are hidden because they cannot hold this
            permit.
          </p>
        ) : (
          <p className="body jo-sub cand-elig-note">
            {state.page.excluded_ineligible.toLocaleString()}{" "}
            {state.page.excluded_ineligible === 1 ? "candidate" : "candidates"} hidden — they cannot
            hold this permit.
          </p>
        ))}

      <p className="body jo-sub cand-elig-note">
        Candidates with incomplete details — like a missing date of birth — are still shown here;
        that is not the same as being ineligible.
      </p>
    </div>
  );
}

const EMPTY: Candidate[] = [];

function emptyLine(filter: Filter, initial: string | null, scope: Scope): string {
  // The letter comes first: with one selected, "Nobody is at this stage yet."
  // is simply untrue — someone may well be, just not under this letter.
  if (initial === NON_ALPHA_INITIAL) return "No names outside A–Z match the rest of your filters.";
  if (initial) return `No names beginning with ${initial} match the rest of your filters.`;
  if (filter === "merged") return "Nothing has been merged.";
  if (filter) return "Nobody is at this stage yet.";
  // Before the last line, because that line invites adding the first candidate
  // to an agency — which under "Mine" or "Queue" is very probably not empty at
  // all, and telling a recruiter their agency has no candidates because their
  // own desk is clear is the exact misreading the scope row exists to prevent.
  if (scope === "mine") return "You do not hold any candidates yet. Claim one from the queue, or add someone.";
  if (scope === "queue") return "Nothing is waiting in the queue — every candidate has a recruiter.";
  if (scope === "shared_with_me") return "No colleague has shared a candidate with you.";
  return "No candidates yet. Add the first one, or import a spreadsheet once that lands.";
}

function Pager({
  total,
  limit,
  offset,
  onOffset,
}: {
  total: number;
  limit: number;
  offset: number;
  onOffset: (offset: number) => void;
}) {
  if (total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));

  return (
    <nav className="jo-pager" aria-label="Candidate pages">
      <button
        type="button"
        className="btn btn-secondary"
        disabled={offset === 0}
        onClick={() => onOffset(Math.max(0, offset - limit))}
      >
        Previous
      </button>
      <span className="body jo-sub" aria-live="polite">
        Page {page} of {pages}
      </span>
      <button
        type="button"
        className="btn btn-secondary"
        disabled={offset + limit >= total}
        onClick={() => onOffset(offset + limit)}
      >
        Next
      </button>
    </nav>
  );
}
