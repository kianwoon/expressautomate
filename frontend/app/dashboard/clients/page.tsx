"use client";

import { useCallback, useEffect, useState } from "react";

import { CLIENTS_PAGE_SIZES, LANDING_PATH } from "../../api";
import { useAuth } from "../../auth";
import { SiteFooter } from "../../site-footer";
import { SiteNav } from "../../site-nav";
import { archiveClient, confirmClient, getClient, restoreClient, useClients } from "../clients";
import type { Client, ClientSort, Filter } from "../clients";
import { ClientForm } from "./client-form";
import "./clients.css";
import { ClientPanel } from "./client-panel";
import { ClientsTable } from "./clients-table";

/**
 * The signed-in screen for the companies a job order came from.
 *
 * The review queue is still the centre of it: the ingestion pipeline proposes
 * a client for every job-order email, landing `unconfirmed`, and
 * confirm/merge/archive are deliberately human-only. **Add client** sits
 * beside the chips because a client that has never sent an email — one you
 * were introduced to, one that only ever phones — is still a client, and
 * waiting for an email before it can be recorded is not a rule anyone asked
 * for. There is no role gate: every action here is open to any signed-in
 * user, the same as everywhere else in the dashboard.
 *
 * Composed exactly like `dashboard/candidates/page.tsx`: the same `useAuth()`
 * and the same anonymous-user redirect.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the
 * page, not a list anything is matched against.
 */

const CHIPS: { key: Filter; label: string }[] = [
  { key: "unconfirmed", label: "Unconfirmed" },
  { key: null, label: "All" },
  { key: "confirmed", label: "Confirmed" },
  // A live client on hold, not a dead one. It sits between Confirmed and
  // Archived here because that is where it sits in the lifecycle, and it is
  // counted inside "All" — suspending a client does not make it disappear.
  { key: "suspended", label: "Suspended" },
  { key: "archived", label: "Archived" },
  // Its own chip, and load-bearing: merged rows are hidden from the default
  // list and the pointer runs loser -> survivor, so this is the only route
  // back to a wrongly merged client.
  { key: "merged", label: "Merged" },
];

// The A–Z bar mirrors the candidates page verbatim. `#` is every name whose
// first character is not a Latin letter — digits, punctuation, CJK — exactly
// the bucket the server folds such names into.
const NON_ALPHA_INITIAL = "#";
const INITIALS: string[] = [
  ...Array.from({ length: 26 }, (_, i) => String.fromCharCode(65 + i)),
  NON_ALPHA_INITIAL,
];

export default function ClientsPage() {
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
              <Workspace />
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

function Workspace() {
  const {
    state,
    filter,
    offset,
    limit: pageSize,
    counts,
    q,
    setQ,
    initial,
    initials,
    setInitial,
    sort,
    setSort,
    setFilter,
    setOffset,
    setLimit,
    reload,
  } = useClients();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Client | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const items = state.status === "ready" ? state.page.items : EMPTY;
  const total = state.status === "ready" ? state.page.total : 0;
  const limit = state.status === "ready" ? state.page.limit : pageSize;

  // Built once and rendered in one of two places, exactly as `job-orders.tsx`
  // and the candidates page do: under the table when there are rows, and on
  // its own when the page turned out to be empty — which a poll, an archive
  // or a merge can produce out from under whoever is standing on a later
  // page, and the pager is the only way back.
  const pager = (
    <Pager
      total={total}
      limit={limit}
      offset={offset}
      pageSize={pageSize}
      onOffset={(next) => {
        setOffset(next);
        setSelectedId(null);
      }}
      onPageSize={(next) => {
        setLimit(next);
        // Same reason as paging: resizing the page goes back to the first
        // one, so the open row is very likely not on it.
        setSelectedId(null);
      }}
    />
  );

  // The detail is a modal opened on demand, so there is no always-open panel
  // to keep fed: `selectedId` is null until a row is clicked, and the modal is
  // mounted only while it is set. The earlier "open on the first row" fallback
  // existed to keep an always-visible panel from starting blank — no longer
  // needed once the panel only appears when asked for.

  // The panel needs the full record — mentions are only on the single-record
  // GET, never on a list row — so it is fetched separately whenever the
  // selection changes.
  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailError(null);
    (async () => {
      try {
        const full = await getClient(selectedId);
        if (!cancelled) setDetail(full);
      } catch {
        if (!cancelled) setDetailError("We could not load that client just now.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  // Re-reads the one open record and nothing else. The logo is deliberately
  // not drawn in the table, so a changed `logo_key` cannot alter a visible
  // row and there is nothing for the list to learn from refetching it.
  const refetchDetail = useCallback(() => {
    if (!selectedId) return;
    getClient(selectedId)
      .then(setDetail)
      .catch(() => setDetailError("We could not load that client just now."));
  }, [selectedId]);

  // Confirm, archive, restore, suspend, merge and edit all move a row between
  // filters or change what the list shows, so those genuinely do need the
  // list re-read as well.
  const refreshDetail = useCallback(() => {
    reload();
    refetchDetail();
  }, [reload, refetchDetail]);

  // Picking a letter or a new sort column both re-order the list from its
  // first page, so the open row — very likely no longer at the top — is closed
  // for the same reason paging closes it. A search clears it for the same
  // reason: a narrower list is a different first page.
  const pickInitial = useCallback(
    (next: string | null) => {
      setInitial(next);
      setSelectedId(null);
    },
    [setInitial],
  );
  const onSort = useCallback(
    (next: ClientSort) => {
      setSort(next);
      setSelectedId(null);
    },
    [setSort],
  );

  async function doConfirm() {
    if (!detail) return;
    await confirmClient(detail.id);
    refreshDetail();
  }

  async function doArchive() {
    if (!detail) return;
    await archiveClient(detail.id);
    refreshDetail();
  }

  async function doRestore() {
    if (!detail) return;
    await restoreClient(detail.id);
    refreshDetail();
  }

  return (
    <>
      {/* No eyebrow above the heading: "CLIENTS" over "The companies behind
          your job orders." labelled the page twice, and the nav already says
          which page this is. The heading starts the page. */}
      <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>
        The companies behind your job orders.
      </h1>
      {/* The list below is full width — `.jo-list` fills the wrap — so the lede
          matches it instead of stopping at `.lede`'s 62ch. Same move the
          candidates page made when its detail became a modal. */}
      <p className="lede cl-lede" style={{ marginTop: 18 }}>
        Most of these were proposed automatically from an email. Confirm the ones that are real,
        archive what is not, and merge duplicates as they turn up — and add one yourself when a
        client arrives some other way.
      </p>

      <div className="jo-controls jo-controls-split" style={{ marginTop: 24 }}>
        <div className="jo-chips" role="group" aria-label="Filter clients">
          {CHIPS.map((chip) => {
            const active = filter === chip.key;
            const n = chip.key === null ? counts.all : counts[chip.key];
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
                {n != null && <span className="jo-chip-n">{n.toLocaleString()}</span>}
              </button>
            );
          })}
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setAdding(true)}>
          Add client
        </button>
      </div>

      <div className="jo-controls" style={{ marginTop: 12 }}>
        <input
          className="jo-search"
          type="search"
          value={q}
          onChange={(event) => setQ(event.target.value)}
          placeholder="Search name…"
          aria-label="Search clients"
        />
      </div>

      <nav className="jo-index" aria-label="Jump to clients by first letter">
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
              // `initials` is the server's word on which letters have rows, so
              // a letter with none is disabled rather than hidden: the bar is a
              // map of the alphabet, and gaps in it would read as bugs.
              disabled={!initials.includes(letter)}
              onClick={() => pickInitial(letter)}
            >
              {letter}
            </button>
          );
        })}
      </nav>

      {adding && (
        <ClientForm
          client={null}
          onCancel={(createdId) => {
            setAdding(false);
            // A plain cancel with nothing created is a no-op, same as
            // before. But if create had already succeeded and only a
            // contact call afterwards failed, the client is real on the
            // server the moment `createdId` is set — reload so it is not
            // left invisible, and select it so Cancel does not read as
            // "nothing happened" and invite a duplicate.
            if (createdId) {
              setFilter(null);
              setSelectedId(createdId);
              reload();
            }
          }}
          onDone={(saved) => {
            setAdding(false);
            // Straight to the record that was just created, rather than
            // leaving the recruiter to find it: a manual client lands
            // `confirmed`, so the default `unconfirmed` chip would not show it
            // and the add would look as though it had not worked.
            setFilter(null);
            setSelectedId(saved.id);
            reload();
          }}
        />
      )}

      {state.status === "loading" ? (
        <p className="body jo-note">Loading your clients.</p>
      ) : state.status === "unreadable" ? (
        <p className="body jo-note" role="alert">
          {state.message}
        </p>
      ) : items.length === 0 ? (
        <>
          <p className="body jo-note" aria-live="polite">
            {offset > 0
              ? // Not "there are none" — there are, on an earlier page. The
                // list shrank while this page was open, the same case
                // `job-orders.tsx` and the candidates page handle the same
                // way.
                "This page is empty now. The list changed while you were reading it."
              : emptyLine(filter)}
          </p>
          {/* An empty page that is not the first one. The pager is how a
              recruiter standing on it gets back — see the note on `pager`
              above. */}
          {offset > 0 && pager}
        </>
      ) : (
        <>
          <p className="body jo-note" aria-live="polite">
            Showing {offset + 1}–{offset + items.length} of {total.toLocaleString()}.
          </p>
          {/* Full width: the table and its pager together, the same `.jo-list`
              shape the job orders and candidates pages use. The detail opens
              as a modal over the list rather than beside it, so the table
              keeps the whole row whether a client is open or not. */}
          <div className="jo-list">
            <ClientsTable
              rows={items}
              selectedId={selectedId}
              onSelect={setSelectedId}
              sort={sort}
              onSort={onSort}
            />
            {pager}
          </div>
          {/* Mounted only while a row is open. The modal owns nothing about the
              selection — clearing it (Escape, backdrop, paging) is the parent's
              job, done through `setSelectedId(null)`. A fetch error shows the
              message beside the list rather than opening an empty modal. */}
          {selectedId && detail && !detailError && (
            <ClientPanel
              key={detail.id}
              row={detail}
              onClose={() => setSelectedId(null)}
              onConfirm={doConfirm}
              onArchive={doArchive}
              onRestore={doRestore}
              onChanged={refreshDetail}
              onDetailChanged={refetchDetail}
              onSelectClient={setSelectedId}
            />
          )}
          {detailError && (
            <p className="body jo-detail-error" role="alert">
              {detailError}
            </p>
          )}
        </>
      )}
    </>
  );
}

const EMPTY: Client[] = [];

function emptyLine(filter: Filter): string {
  if (filter === "merged") return "Nothing has been merged.";
  if (filter === "unconfirmed") return "Nothing is waiting on you. New proposals appear here as job-order emails arrive.";
  if (filter === "suspended") return "No client is on hold.";
  if (filter) return "No clients at this status yet.";
  return "No clients yet. They appear automatically as job-order emails are ingested — or add the first one yourself.";
}

/** `limit` is what the server used and is what the arithmetic must run on —
 *  it clamps, so it is not always what was asked for. `pageSize` is what was
 *  asked for, and is the only thing the control may show: a select whose
 *  value disagrees with its options renders blank. Mirrors `Pager` in
 *  `job-orders.tsx` and `dashboard/candidates/page.tsx` exactly, down to this
 *  comment — same distinction, same reason.
 *
 *  The size control stays even when everything fits on one page — that is
 *  exactly when someone might want a smaller one — so only Previous/Next and
 *  the page count are conditional. This is the one place clients used to
 *  diverge from the other two screens: the old `Pager` returned `null`
 *  entirely below one page, which is also what hid the size control from
 *  someone who never needed Previous or Next in the first place. */
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
  // under a reload leaves someone standing on page 2 of a set that now fits
  // on page 1, and "Page 2 of 1" is not a thing to show anyone.
  const pages = Math.max(1, Math.ceil(total / limit), page);
  // `offset > 0` and not only `total > limit`: in that same case there is one
  // page's worth of rows and the reader is past it, so the steps are the only
  // way back — hiding them because everything now fits is what would strand
  // them.
  const paged = total > limit || offset > 0;

  return (
    <nav className="jo-pager" aria-label="Client pages and page size">
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
          {CLIENTS_PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </label>
    </nav>
  );
}
