"use client";

import { useEffect, useState } from "react";

import { OPPORTUNITIES_PAGE_SIZE, OPPORTUNITIES_PAGE_SIZES } from "../api";
import type { Me } from "../auth";
import { DetailPanel } from "./detail-panel";
import "./job-orders.css";
import { LiveLight } from "./live-light";
import { JobOrdersTable } from "./job-orders-table";
import {
  assignOpportunity,
  claimOpportunity,
  getOpportunity,
  useOpportunities,
  type Filter,
  type MutationResult,
  type Opportunity,
  type Scope,
} from "./opportunities";
import { ReviewBell, StatCards } from "./stat-cards";
import { MailboxOverview, SyncActivity } from "./sync-activity";

/**
 * The signed-in workspace: the numbers, the filters, the list, and the one row
 * someone is reading.
 *
 * It owns the fetch because three things need the same response. The stat
 * cards need `counts`, the chips need `counts`, and the table needs `items` —
 * fetched separately they would disagree with each other within a second of
 * anyone marking a row reviewed, and a dashboard whose header contradicts its
 * own table is worse than one that shows less.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
 */

const CHIPS: { key: Filter; label: string; countKey: "all" | "new" | "needs_review" | "reviewed" }[] =
  [
    { key: null, label: "All", countKey: "all" },
    { key: "new", label: "New", countKey: "new" },
    { key: "needs_review", label: "Needs review", countKey: "needs_review" },
    { key: "reviewed", label: "Reviewed", countKey: "reviewed" },
  ];

/**
 * Whose job orders, which is a different question from what state they are in.
 *
 * Two rows rather than one merged list, because the two are independent axes: a
 * recruiter wanting "mine, needing review" is asking one question and must not
 * have to choose which half of it to ask. Merging them into a single row would
 * make every combination its own chip — sixteen of them — and would still make
 * the two look mutually exclusive.
 *
 * No count on these. The endpoint returns counts per review status, not per
 * scope; a number worked out in the browser would be a count of the page rather
 * than of the set, and would disagree with the server on exactly the rows that
 * made someone look.
 */
const SCOPES: { key: Scope; label: string }[] = [
  { key: "mine", label: "Mine" },
  { key: "queue", label: "Queue" },
  { key: "shared_with_me", label: "Shared with me" },
  { key: "all", label: "All" },
];

export function JobOrders({ me, heading = "h2" }: { me: Me; heading?: "h1" | "h2" }) {
  const Heading = heading;

  const {
    state,
    filter,
    scope,
    offset,
    limit: pageSize,
    q,
    sort,
    counts,
    refreshing,
    setFilter,
    setScope,
    setOffset,
    setLimit,
    setQ,
    setSort,
    review,
    patchRow,
  } = useOpportunities();
  // The whole row, not the id. Marking something reviewed under a "Needs
  // review" filter can take it out of the page it was selected from, and the
  // panel someone is mid-way through reading should not empty itself as a
  // reward for using it.
  const [selected, setSelected] = useState<Opportunity | null>(null);

  /** One ownership move, then the row read back from the server.
   *
   * Read back rather than patched in place: the new assignee's *name* is
   * resolved server-side, and inventing one here would be a second place
   * deciding what a colleague is called. A failed read-back is swallowed —
   * the move itself succeeded, and the next poll brings the row round.
   *
   * The row goes into the *list* as well as into the panel, and both halves
   * are needed. The panel alone would be undone within the same tick: the sync
   * effect below finds the pre-claim object still sitting in `items` and pins
   * the selection back to it, so the new owner appears and vanishes again.
   * The list alone would leave the panel a poll behind. Written to both, the
   * effect finds the row it already has and re-pinning is a no-op. */
  async function own(id: string, run: () => Promise<MutationResult>): Promise<MutationResult> {
    const result = await run();
    if (!result.ok) return result;
    try {
      const fresh = await getOpportunity(id);
      patchRow(fresh);
      setSelected(fresh);
    } catch {
      /* the next poll will bring it */
    }
    return result;
  }

  const items = state.status === "ready" ? state.page.items : EMPTY;

  // The selected row has to follow the data. Without this, marking a row
  // reviewed updates the table underneath a panel still showing the old badge
  // and still offering "Mark as reviewed".
  useEffect(() => {
    if (!selected) return;
    const fresh = items.find((row) => row.id === selected.id);
    if (fresh && fresh !== selected) setSelected(fresh);
  }, [items, selected]);

  // Open on the first row rather than on an empty panel: a panel that starts
  // blank spends the first screenful asking to be clicked.
  //
  // Derived, never stored. An effect that selected the first row on seeing an
  // empty selection would fire on the paging render too — `setOffset` and
  // `setSelected(null)` batch together, so that render still holds the page
  // being left, and the effect would store one of its rows just as the fetch
  // for the next page began. Nothing would clear it afterwards, and the panel
  // would sit on a row the table no longer lists. Falling back during render
  // has nothing to go stale: while the next page loads there are no rows and
  // the fallback is empty, and when it arrives the fallback is its first row.
  const shown = selected ?? items[0] ?? null;

  const total = state.status === "ready" ? state.page.total : 0;
  const limit = state.status === "ready" ? state.page.limit : OPPORTUNITIES_PAGE_SIZE;
  // The server has already filtered by `q`; a page shorter than the full set
  // is what "some rows matched" looks like now, not something computed here.
  const searched = q.trim().length > 0;

  // Built once and rendered in one of two places: under the table when there
  // are rows, and on its own when the page turned out to be empty. Two call
  // sites with the same six props is two things to keep in step.
  const pager = (
    <Pager
      total={total}
      limit={limit}
      offset={offset}
      pageSize={pageSize}
      onOffset={(next) => {
        setOffset(next);
        // The panel is showing a row from the page being left behind.
        setSelected(null);
      }}
      onPageSize={(next) => {
        setLimit(next);
        // Same reason as paging: resizing the page goes back to the first one,
        // so the open row is very likely not on it.
        setSelected(null);
      }}
    />
  );

  return (
    <section className="jo-workspace" data-lead={heading === "h1" ? "yes" : undefined}>
      <StatCards me={me} counts={counts} />

      <div className="jo-head">
        <div>
          <span className="eyebrow">Job orders</span>
          {/* The page's h1 when nothing above it is one. Removing "Signed in
              as …" left a running dashboard whose outline began at h2, which
              is how a screen reader loses the top of a document. This is the
              heading that describes the page, so it is the one to promote —
              rather than hiding an h1 nobody sees to satisfy the outline. */}
          <Heading className="jo-head-title">Every vacancy we have read.</Heading>
        </div>
        {/* Beside the heading of the thing it vouches for. The table below is
            what goes stale when the stream stops, so the light saying so
            belongs here rather than in a corner of the page. */}
        <LiveLight />
        <ReviewBell count={counts.needs_review} onOpen={() => setFilter("needs_review")} />
      </div>

      <p className="body jo-head-body">
        Each row is what one email actually said — where it said nothing, it says so rather than
        guessing. The first one is open beside the list; select any other to read that instead.
      </p>

      <div className="jo-controls">
        <div className="jo-chips" role="group" aria-label="Filter job orders">
          {CHIPS.map((chip) => {
            const active = filter === chip.key;
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
                <span className="jo-chip-n">{counts[chip.countKey].toLocaleString()}</span>
              </button>
            );
          })}
        </div>

        <input
          className="jo-search"
          type="search"
          value={q}
          onChange={(event) => setQ(event.target.value)}
          placeholder="Search company, role, salary, location…"
          aria-label="Search all job orders"
        />
      </div>

      {/* Its own row, under the status chips rather than beside them. Sharing
          the row would put the two questions on one line with the search box
          and read as one long list of alternatives — which is the one thing
          they are not. */}
      <div className="jo-scopes">
        <div className="jo-chips" role="group" aria-label="Filter job orders by owner">
          {SCOPES.map((chip) => {
            const active = scope === chip.key;
            return (
              <button
                key={chip.key}
                type="button"
                className="jo-chip"
                data-active={active ? "yes" : undefined}
                aria-pressed={active}
                onClick={() => setScope(chip.key)}
              >
                <span>{chip.label}</span>
              </button>
            );
          })}
        </div>
      </div>

      {state.status === "loading" ? (
        <p className="body jo-note">Loading your job orders.</p>
      ) : state.status === "unreadable" ? (
        // Never rendered as an empty list. A failed fetch shown as "no job
        // orders" tells a recruiter their mailbox found nothing.
        <p className="body jo-note" role="alert">
          {state.message}
        </p>
      ) : (
        <>
          <p className="body jo-note" aria-live="polite">
            {items.length === 0
              ? searched
                ? `Nothing matches “${q.trim()}”.`
                : offset > 0
                  ? // Not "there are none" — there are, on an earlier page.
                    // The list shrank while this page was open.
                    "This page is empty now. The list changed while you were reading it."
                  : emptyLine(filter)
              : searched
                ? `${total.toLocaleString()} match${total === 1 ? "" : "es"} “${q.trim()}”, including the requirements and description.`
                : `Showing ${offset + 1}–${offset + items.length} of ${total.toLocaleString()}.`}
          </p>

          {items.length === 0 && searched && (
            <div className="card jo-note-card">
              <p className="body">
                Nothing mentions <strong>{q.trim()}</strong>. Only what the emails themselves said
                is searchable — a detail the sender left out is not in here to be found.
              </p>
              <button type="button" className="btn btn-secondary" onClick={() => setQ("")}>
                Clear the search
              </button>
            </div>
          )}

          {items.length > 0 && (
            <div className="jo-split" aria-busy={refreshing || undefined}>
              {/* The pager lives in this column, under the table it steps
                  through. Below the split it sat under the detail panel too,
                  which is sticky and a screen tall, so the control for the
                  list ended up nowhere near the list. */}
              <div className="jo-list">
                <JobOrdersTable
                  rows={items}
                  sort={sort}
                  onSort={setSort}
                  selectedId={shown?.id ?? null}
                  onSelect={setSelected}
                />
                {pager}
              </div>
              {/* Reviewing the row the fallback offered pins it first. Under
                  "Needs review" the act of reviewing takes it out of the list,
                  and an unpinned fallback would slide the panel onto the next
                  row mid-read — the same disappearing-panel the stored
                  selection above exists to prevent. */}
              <DetailPanel
                row={shown}
                onReview={(id, reviewed) => {
                  if (!selected && shown) setSelected(shown);
                  return review(id, reviewed);
                }}
                onClaim={(id) => {
                  if (!selected && shown) setSelected(shown);
                  return own(id, () => claimOpportunity(id));
                }}
                onAssign={(id, userId) => {
                  if (!selected && shown) setSelected(shown);
                  return own(id, () => assignOpportunity(id, userId));
                }}
                // The row went out from under the panel. Dropping the pinned
                // selection is what lets the next poll choose a row that still
                // exists, instead of the panel reopening on what is gone.
                onVanished={() => setSelected(null)}
              />
            </div>
          )}

          {/* An empty page that is not the first one. A poll re-reads the
              offset the reader is standing on, so a colleague reviewing the
              last rows of page 2 under "Needs review" empties it underneath
              them. The pager is how they get back, and it is inside the split
              above, which this state does not render. */}
          {items.length === 0 && offset > 0 && pager}
        </>
      )}

      <div className="jo-panels">
        <SyncActivity />
        <MailboxOverview me={me} />
      </div>
    </section>
  );
}

/** A stable identity for "no rows yet", so the memo above is not recomputing
 *  against a fresh array on every render while the fetch is still out. */
const EMPTY: Opportunity[] = [];

/** An empty filter and an empty database are different facts. */
function emptyLine(filter: Filter): string {
  if (filter === "needs_review") return "Nothing is waiting for review.";
  if (filter === "reviewed") return "Nothing has been marked reviewed yet.";
  if (filter === "new") return "Nothing new. Everything read so far has been looked at.";
  return "No job orders yet. They appear here as emails are read and understood.";
}

/** `limit` is what the server used and is what the arithmetic must run on — it
 *  clamps, so it is not always what was asked for. `pageSize` is what was
 *  asked for, and is the only thing the control may show: a select whose value
 *  disagrees with its options renders blank.
 *
 *  The size control stays even when everything fits on one page — that is
 *  exactly when someone might want a smaller one — so only Previous/Next and
 *  the page count are conditional. */
function Pager({
  total,
  limit,
  offset,
  pageSize,
  onOffset,
  onPageSize,
}: {
  total: number;
  limit: number;
  offset: number;
  pageSize: number;
  onOffset: (offset: number) => void;
  onPageSize: (limit: number) => void;
}) {
  const page = Math.floor(offset / limit) + 1;
  // `page` is in the max because the two can disagree: a list that shrank
  // under a poll leaves someone standing on page 2 of a set that now fits on
  // page 1, and "Page 2 of 1" is not a thing to show anyone.
  const pages = Math.max(1, Math.ceil(total / limit), page);
  // `offset > 0` and not only `total > limit`: in that same case there is one
  // page's worth of rows and the reader is past it, so the steps are the only
  // way back — hiding them because everything now fits is what would strand
  // them.
  const paged = total > limit || offset > 0;

  return (
    <nav className="jo-pager" aria-label="Job order pages and page size">
      {paged && (
        <>
          <button
            type="button"
            className="btn btn-secondary"
            disabled={offset === 0}
            onClick={() => onOffset(Math.max(0, offset - limit))}
          >
            Previous
          </button>
          {/* Said in words, not only implied by which button is disabled: a
              disabled button is not an announcement, and "page 3 of 9" is what
              someone actually wants to know. */}
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
        </>
      )}
      <label className="jo-perpage">
        Rows per page
        <select
          className="jo-perpage-select"
          value={pageSize}
          onChange={(event) => onPageSize(Number(event.target.value))}
        >
          {OPPORTUNITIES_PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </label>
    </nav>
  );
}
