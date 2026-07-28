"use client";

import { useEffect, useMemo, useState } from "react";

import type { Me } from "../auth";
import { DetailPanel } from "./detail-panel";
import { compare, haystack } from "./job-orders-sort";
import { DEFAULT_SORT, JobOrdersTable, type Sort } from "./job-orders-table";
import { PAGE_SIZE, useOpportunities, type Filter, type Opportunity } from "./opportunities";
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

export function JobOrders({ me, heading = "h2" }: { me: Me; heading?: "h1" | "h2" }) {
  const Heading = heading;

  const { state, filter, offset, counts, setFilter, setOffset, review } = useOpportunities();
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>(DEFAULT_SORT);
  // The whole row, not the id. Marking something reviewed under a "Needs
  // review" filter can take it out of the page it was selected from, and the
  // panel someone is mid-way through reading should not empty itself as a
  // reward for using it.
  const [selected, setSelected] = useState<Opportunity | null>(null);

  const items = state.status === "ready" ? state.page.items : EMPTY;

  // Filter first, then sort, and never in place. `.sort` mutates its receiver,
  // so sorting the fetched array directly would reorder the array React holds
  // as state — the next render compares it to itself, sees no change, and
  // paints the old order.
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matched = needle ? items.filter((row) => haystack(row).includes(needle)) : items;
    return [...matched].sort((a, b) => compare(a, b, sort));
  }, [items, query, sort]);

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
  const shown = selected ?? visible[0] ?? null;

  const total = state.status === "ready" ? state.page.total : 0;
  const limit = state.status === "ready" ? state.page.limit : PAGE_SIZE;
  const filtered = visible.length !== items.length;

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
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search company, role, salary, location…"
          aria-label="Search the job orders on this page"
        />
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
              ? emptyLine(filter)
              : filtered
                ? `${visible.length} of ${items.length} on this page match “${query.trim()}”. The search covers this page, including the requirements and description, not all ${total.toLocaleString()}.`
                : `Showing ${offset + 1}–${offset + items.length} of ${total.toLocaleString()}.`}
          </p>

          {items.length > 0 && visible.length === 0 && (
            <div className="card jo-note-card">
              <p className="body">
                Nothing on this page mentions <strong>{query.trim()}</strong>. Only what the emails
                themselves said is searchable — a detail the sender left out is not in here to be
                found.
              </p>
              <button type="button" className="btn btn-secondary" onClick={() => setQuery("")}>
                Clear the search
              </button>
            </div>
          )}

          {visible.length > 0 && (
            <div className="jo-split">
              <JobOrdersTable
                rows={visible}
                sort={sort}
                onSort={setSort}
                selectedId={shown?.id ?? null}
                onSelect={setSelected}
              />
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
              />
            </div>
          )}

          <Pager
            total={total}
            limit={limit}
            offset={offset}
            onOffset={(next) => {
              setOffset(next);
              // The panel is showing a row from the page being left behind.
              setSelected(null);
            }}
          />
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
    <nav className="jo-pager" aria-label="Job order pages">
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
    </nav>
  );
}
