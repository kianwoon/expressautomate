"use client";

import { useCallback, useEffect, useState } from "react";

import { LANDING_PATH } from "../../api";
import { useAuth } from "../../auth";
import { SiteFooter } from "../../site-footer";
import { SiteNav } from "../../site-nav";
import { archiveClient, confirmClient, getClient, restoreClient, useClients } from "../clients";
import type { Client, Filter } from "../clients";
import { ClientPanel } from "./client-panel";
import { ClientsTable } from "./clients-table";

/**
 * The signed-in screen for the companies a job order came from.
 *
 * There is no "Add client" button and no role gate — the whole reason this
 * screen exists is that the ingestion pipeline already proposes a client for
 * every job-order email, landing `unconfirmed`, and confirm/merge/archive
 * are deliberately human-only. Before this screen that queue rendered
 * nowhere; this makes it reachable, and open to any signed-in user, the same
 * as every other action here.
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
  { key: "archived", label: "Archived" },
  // Its own chip, and load-bearing: merged rows are hidden from the default
  // list and the pointer runs loser -> survivor, so this is the only route
  // back to a wrongly merged client.
  { key: "merged", label: "Merged" },
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
                eyebrow="Connection"
                heading="We could not reach the server."
                body="This is not a sign-in problem — your session is untouched. Reload the page in a moment."
              />
            ) : auth.status === "anonymous" ? (
              <Notice eyebrow="Signed out" heading="Taking you back." body="You are not signed in. Returning to the home page." />
            ) : (
              <Notice eyebrow="Checking" heading="One moment." body="Checking your session." />
            )}
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}

function Notice({ eyebrow, heading, body }: { eyebrow: string; heading: string; body: string }) {
  return (
    <>
      <span className="eyebrow">{eyebrow}</span>
      <h1 style={{ marginTop: 14, fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>{heading}</h1>
      <p className="lede" style={{ marginTop: 18 }}>
        {body}
      </p>
    </>
  );
}

function Workspace() {
  const { state, filter, offset, counts, setFilter, setOffset, reload } = useClients();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Client | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const items = state.status === "ready" ? state.page.items : EMPTY;
  const total = state.status === "ready" ? state.page.total : 0;
  const limit = state.status === "ready" ? state.page.limit : 50;

  // Open on the first row once the page loads, same fallback as candidates:
  // a panel that starts blank spends the first screenful asking to be
  // clicked, and here that row is the top of the review queue.
  useEffect(() => {
    if (!selectedId && items.length > 0) setSelectedId(items[0].id);
  }, [items, selectedId]);

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

  const refreshDetail = useCallback(() => {
    reload();
    if (selectedId) {
      getClient(selectedId)
        .then(setDetail)
        .catch(() => setDetailError("We could not load that client just now."));
    }
  }, [reload, selectedId]);

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
      <p className="lede" style={{ marginTop: 18 }}>
        Every one of these was proposed automatically from an email — nothing here was typed by
        hand. Confirm the ones that are real, archive what is not, and merge duplicates as they
        turn up.
      </p>

      <div className="jo-controls" style={{ marginTop: 24 }}>
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
      </div>

      {state.status === "loading" ? (
        <p className="body jo-note">Loading your clients.</p>
      ) : state.status === "unreadable" ? (
        <p className="body jo-note" role="alert">
          {state.message}
        </p>
      ) : items.length === 0 ? (
        <p className="body jo-note" aria-live="polite">
          {emptyLine(filter)}
        </p>
      ) : (
        <>
          <p className="body jo-note" aria-live="polite">
            Showing {offset + 1}–{offset + items.length} of {total.toLocaleString()}.
          </p>
          <div className="jo-split">
            <ClientsTable rows={items} selectedId={selectedId} onSelect={setSelectedId} />
            <ClientPanel
              row={detailError ? null : detail}
              onConfirm={doConfirm}
              onArchive={doArchive}
              onRestore={doRestore}
              onChanged={refreshDetail}
              onSelectClient={setSelectedId}
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

const EMPTY: Client[] = [];

function emptyLine(filter: Filter): string {
  if (filter === "merged") return "Nothing has been merged.";
  if (filter === "unconfirmed") return "Nothing is waiting on you. New proposals appear here as job-order emails arrive.";
  if (filter) return "No clients at this status yet.";
  return "No clients yet. They appear automatically as job-order emails are ingested.";
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
    <nav className="jo-pager" aria-label="Client pages">
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
