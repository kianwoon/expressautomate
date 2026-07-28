"use client";

import { useCallback, useEffect, useState } from "react";

import { LANDING_PATH } from "../../api";
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
import type { Candidate, Filter } from "../candidates";
import { CandidateForm } from "./candidate-form";
import { CandidatePanel } from "./candidate-panel";
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

type View = { mode: "list" } | { mode: "create" } | { mode: "edit"; row: Candidate };

function Workspace({ role }: { role: string }) {
  const { state, filter, offset, q, counts, refreshing, setFilter, setOffset, setQ, reload } =
    useCandidates();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Candidate | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [view, setView] = useState<View>({ mode: "list" });

  const items = state.status === "ready" ? state.page.items : EMPTY;
  const total = state.status === "ready" ? state.page.total : 0;
  const limit = state.status === "ready" ? state.page.limit : 50;

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

  const canDelete = role === "owner";
  async function doDelete() {
    if (!detail) return;
    await deleteCandidate(detail.id);
    setSelectedId(null);
    setDetail(null);
    reload();
  }

  if (view.mode !== "list") {
    return (
      <CandidateForm
        row={view.mode === "edit" ? view.row : null}
        onCancel={() => setView({ mode: "list" })}
        onDone={(saved) => {
          setView({ mode: "list" });
          setSelectedId(saved.id);
          reload();
        }}
      />
    );
  }

  return (
    <>
      <span className="eyebrow">Candidates</span>
      <h1 style={{ marginTop: 14, fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>
        The people you place.
      </h1>
      <p className="lede" style={{ marginTop: 18 }}>
        Every candidate your agency has recorded, tenant-wide — nothing here came from an email;
        it was typed by a person or came from a sheet a person uploaded.
      </p>

      <div className="jo-head" style={{ marginTop: 24 }}>
        <button type="button" className="btn btn-primary" onClick={() => setView({ mode: "create" })}>
          Add candidate
        </button>
      </div>

      <div className="jo-controls" style={{ marginTop: 20 }}>
        <div className="jo-chips" role="group" aria-label="Filter candidates">
          {CHIPS.map((chip) => {
            const active = filter === chip.key;
            const n = chip.key === null ? counts.all : chip.key === "merged" ? null : counts[chip.key];
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

      {state.status === "loading" ? (
        <p className="body jo-note">Loading your candidates.</p>
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

const EMPTY: Candidate[] = [];

function emptyLine(filter: Filter): string {
  if (filter === "merged") return "Nothing has been merged.";
  if (filter) return "Nobody is at this stage yet.";
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
