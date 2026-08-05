"use client";

import { useEffect, useState } from "react";

import { OPPORTUNITIES_PAGE_SIZE, OPPORTUNITIES_PAGE_SIZES } from "../api";
import type { Me } from "../auth";
import { DetailPanel } from "./detail-panel";
import { JobOrderForm } from "./job-order-form";
import "./job-orders.css";
import { IntakePause } from "./intake-pause";
import { LiveLight } from "./live-light";
import { JobOrdersTable } from "./job-orders-table";
import {
  assignOpportunity,
  claimOpportunity,
  getOpportunity,
  setOpportunityClient,
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
    { key: null, label: "All job orders", countKey: "all" },
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
  { key: "all", label: "Everyone" },
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
    dedupe,
    hidden,
    counts,
    refreshing,
    setFilter,
    setScope,
    setOffset,
    setLimit,
    setQ,
    setSort,
    setDedupe,
    review,
    patchRow,
    addRow,
  } = useOpportunities();
  const [creating, setCreating] = useState(false);
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

  /** Puts a just-typed job order in front of the person who typed it.
   *
   * Read back rather than built from the create response, which echoes six
   * fields: the row the table draws has a quality state, a review status and a
   * resolved assignee name, all decided server-side, and inventing them here
   * would be a second place deciding them. If the read-back fails the create
   * still succeeded — the next poll brings the row round, so nothing is said
   * about a problem the recruiter does not have. */
  async function showCreated(id: string): Promise<void> {
    try {
      const fresh = await getOpportunity(id);
      addRow(fresh);
      setSelected(fresh);
    } catch {
      /* the next poll will bring it */
    }
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

  // The detail is a modal opened on demand, so there is no always-open panel
  // to keep fed: `selected` is null until a row is clicked, and the modal is
  // mounted only while it is set. The earlier "open on the first row" fallback
  // existed to keep an always-visible panel from starting blank — no longer
  // needed once the panel only appears when asked for.

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
      {creating && (
        <JobOrderForm onClose={() => setCreating(false)} onCreated={showCreated} />
      )}
      <StatCards me={me} counts={counts} />

      <div className="jo-head">
        <div>
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
        {/* Beside the light, not inside it. `LiveLight` reports whether this
            page is being told things; this switch decides whether the mailbox
            feeds the pipeline at all. Related enough to sit together, and
            unrelated enough that one control for both would lie one way or
            the other. */}
        <IntakePause />
        <ReviewBell count={counts.needs_review} onOpen={() => setFilter("needs_review")} />
        {/* Beside the list it adds to, not in a settings page. A job order
            taken over the phone is typed in while the client is still on it,
            and the button for that belongs where the job orders are. */}
        <button type="button" className="btn btn-secondary" onClick={() => setCreating(true)}>
          New job order
        </button>
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
          {/* A list filter, not a search or a scope: a buddy who re-forwards an
              open job order days later creates a second row for the same role.
              This collapses those later re-forwards, keeping the earliest open
              instance. A separate control from scope because "whose" and
              "deduped" are independent questions. */}
          <button
            type="button"
            className="jo-chip"
            data-active={dedupe ? "yes" : undefined}
            aria-pressed={dedupe}
            onClick={() => setDedupe(!dedupe)}
            title="Hide job orders forwarded again that are still open"
          >
            <span>Hide duplicates</span>
          </button>
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

          {dedupe && hidden > 0 && (
            // The dedupe filter must not drop rows in silence: a recruiter who
            // does not know the list was filtered cannot tell "fewer because
            // filtered" from "fewer because gone".
            <p className="body jo-note muted">
              {hidden} duplicate{hidden === 1 ? "" : "s"} hidden — re-forwards of job
              orders already here and still open. Turn off “Hide duplicates” to see them.
            </p>
          )}

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
            <>
              {/* Full width: the detail now opens as a modal over the list
                  rather than sitting beside it in a split, so the table has the
                  whole row to itself. The pager stays directly under the last
                  row, where it belongs. */}
              <div className="jo-list" aria-busy={refreshing || undefined}>
                <JobOrdersTable
                  rows={items}
                  sort={sort}
                  onSort={setSort}
                  selectedId={selected?.id ?? null}
                  onSelect={setSelected}
                />
                {pager}
              </div>
              {/* Mounted only while a row is open. The modal owns nothing about
                  the selection — clearing it (Escape, backdrop, paging, vanish)
                  is the parent's job, done through `setSelected(null)`. */}
              {selected && (
                <DetailPanel
                  key={selected.id}
                  row={selected}
                  onClose={() => setSelected(null)}
                  onReview={(id, reviewed) => review(id, reviewed)}
                  onClaim={(id) => own(id, () => claimOpportunity(id))}
                  onAssign={(id, userId) => own(id, () => assignOpportunity(id, userId))}
                  // Through `own` like the other two, and for the same reason:
                  // linking a client can hand an unassigned job order to that
                  // client's recruiter, so the row that comes back may name a
                  // different owner. Only the read-back knows.
                  onClientSet={(id, clientId, adopt) =>
                    own(id, async () => {
                      const result = await setOpportunityClient(id, clientId, adopt);
                      // The link itself, applied before the read-back and
                      // regardless of whether the read-back arrives. It is the
                      // one thing the response knows and the browser cannot
                      // work out: the name behind the id it just sent. Without
                      // this a failed read-back leaves a linked job order
                      // looking exactly like an unlinked one.
                      if (result.ok && result.clientName !== undefined) {
                        const linked = {
                          ...selected,
                          client_id: clientId,
                          client_name: result.clientName,
                        };
                        patchRow(linked);
                        setSelected(linked);
                      }
                      return result;
                    })
                  }
                  // The row went out from under the modal. Dropping the selection
                  // unmounts it, rather than leaving it open on what is gone.
                  onVanished={() => setSelected(null)}
                />
              )}
            </>
          )}

          {/* An empty page that is not the first one. A poll re-reads the
              offset the reader is standing on, so a colleague reviewing the
              last rows of page 2 under "Needs review" empties it underneath
              them. The pager is how they get back, and it is inside the list
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
