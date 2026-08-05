"use client";

import { useCallback, useEffect, useId, useState } from "react";

import { BUDDIES_API_PATH, LANDING_PATH } from "../../api";
import { useAuth } from "../../auth";
import { SiteFooter } from "../../site-footer";
import { SiteNav } from "../../site-nav";
import { day } from "../format";
import { Dialog } from "../dialog";

type Buddy = {
  id: string;
  name: string;
  email: string;
  email_domain: string | null;
  phone: string | null;
  source: string;
  referral_count: number;
};

/** The server's list response — a dict, not the bare array the endpoint used
 *  to return. `total` is the length of the filtered set (no pagination here:
 *  a buddy network is small), and `initials` drives the A–Z bar. */
type BuddyPage = {
  items: Buddy[];
  total: number;
  initials: string[];
};

/** The columns the table offers sort headers for. Mirrors the server's
 *  `BuddySortBy` whitelist (`backend/app/api/buddies.py`): the two must
 *  agree. Mobile (phone) is deliberately absent — it is an inline-edit cell,
 *  not something a recruiter scans the list by. */
type BuddySortKey = "name" | "email" | "email_domain" | "referral_count";

type BuddySort = { key: BuddySortKey; descending: boolean };

/** The default view — the buddies who send the most work at the top. The
 *  server falls back to this when no `sort_by` is sent, so the table's
 *  active-column highlight starts honest. `referral_count` rather than the
 *  server's twin-key referral/name: there is one column for referrals in the
 *  UI and the server decides the rest of the tiebreak. */
const DEFAULT_SORT: BuddySort = { key: "referral_count", descending: true };

/** A window the referral count and the referral list can be scoped to. Mirrors
 *  the server's `BuddyPeriod`. `null` is all time — the whole-history view. */
type BuddyPeriod = "7d" | "14d" | "30d";

/** The period chips above the table. `null` first because "All time" is the
 *  default and the rest are in ascending recency, the order a recruiter scans
 *  them in. */
const PERIODS: { key: BuddyPeriod | null; label: string }[] = [
  { key: null, label: "All time" },
  { key: "7d", label: "This week" },
  { key: "14d", label: "2 weeks" },
  { key: "30d", label: "1 month" },
];

/** One row of the referral modal — a compact view of a referred job order.
 *  Carries only the fields a recruiter uses to tell one forwarded job order
 *  from another at a glance, not the full opportunity payload. */
type ReferredJobOrder = {
  id: string;
  job_title_raw: string | null;
  company_name_raw: string | null;
  received_datetime: string | null;
  location_raw: string | null;
  salary_raw: string | null;
};

type ReferralPage = {
  buddy: { id: string; name: string };
  items: ReferredJobOrder[];
  total: number;
};

const NO_INITIALS: string[] = [];

// The A–Z bar mirrors the candidates and clients pages. `#` is every name
// whose first character is not a Latin letter — digits, punctuation, CJK —
// exactly the bucket the server folds such names into.
const NON_ALPHA_INITIAL = "#";
const INITIALS: string[] = [
  ...Array.from({ length: 26 }, (_, i) => String.fromCharCode(65 + i)),
  NON_ALPHA_INITIAL,
];

function listUrl(q: string, initial: string | null, sort: BuddySort, period: BuddyPeriod | null): string {
  const params = new URLSearchParams();
  if (q.trim()) params.set("q", q.trim());
  if (initial) params.set("initial", initial);
  // Always sent, even on the default view: the server's own default order
  // agrees with `DEFAULT_SORT`, so sending it changes nothing there, and it
  // keeps the table's active-column highlight honest. An explicit sort also
  // wins over the letter-browse alphabetical fallback on the server, so a
  // recruiter sorting while standing on a letter gets what they asked for.
  params.set("sort_by", sort.key);
  params.set("descending", String(sort.descending));
  if (period) params.set("period", period);
  return `${BUDDIES_API_PATH}?${params.toString()}`;
}

export default function BuddiesPage() {
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
              <Notice heading="We could not reach the server." body="Reload the page in a moment." />
            ) : auth.status === "anonymous" ? (
              <Notice heading="Taking you back." body="You are not signed in." />
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

function Notice({ heading, body }: { heading: string; body: string }) {
  return (
    <>
      <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>{heading}</h1>
      <p className="lede" style={{ marginTop: 18 }}>{body}</p>
    </>
  );
}

const COLUMNS: { key: BuddySortKey; label: string }[] = [
  { key: "name", label: "Name" },
  { key: "email", label: "Email" },
  // Mobile has no sort header — it is an inline-edit cell, not a column a
  // recruiter scans the list by. Kept out of COLUMNS so it renders as a plain
  // `<th>` alongside the sortable ones.
  { key: "email_domain", label: "Agency" },
  { key: "referral_count", label: "Referrals" },
];

function Workspace() {
  const [page, setPage] = useState<BuddyPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [initial, setInitial] = useState<string | null>(null);
  const [sort, setSort] = useState<BuddySort>(DEFAULT_SORT);
  const [period, setPeriod] = useState<BuddyPeriod | null>(null);
  // The buddy whose referral modal is open, or null. Carries the row snapshot
  // so the dialog title renders before the fetch resolves.
  const [openBuddy, setOpenBuddy] = useState<Buddy | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    // Rows already on screen stay on screen while the refetch runs — the same
    // reasoning as `useClients`. Dropping to `loading` on every keystroke
    // would unmount the inline-edit cell a recruiter might be mid-edit in.
    setPage((prev) => prev);
    (async () => {
      try {
        const res = await fetch(listUrl(q, initial, sort, period), {
          credentials: "include",
          signal: controller.signal,
        });
        if (!res.ok) throw new Error();
        const data = (await res.json()) as BuddyPage;
        setPage(data);
        setError(null);
      } catch {
        if (!controller.signal.aborted) {
          setError("We could not load your buddies just now.");
        }
      }
    })();
    return () => controller.abort();
  }, [q, initial, sort, period]);

  const pickInitial = useCallback((next: string | null) => setInitial(next), []);
  const onSort = useCallback((next: BuddySort) => setSort(next), []);

  const buddies = page?.items ?? [];
  const initials = page?.initials ?? NO_INITIALS;

  if (error) {
    return (
      <>
        <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
        <p className="body jo-detail-error" role="alert" style={{ marginTop: 18 }}>{error}</p>
      </>
    );
  }

  if (page === null) {
    return (
      <>
        <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
        <p className="body jo-note" style={{ marginTop: 18 }}>Loading your buddies.</p>
      </>
    );
  }

  if (buddies.length === 0 && !q && initial === null) {
    return (
      <>
        <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
        <p className="lede" style={{ marginTop: 18, maxWidth: "62ch" }}>
          No buddies yet. When an external recruiter forwards a job order into your mailbox, they
          appear here — linked to the clients they referred.
        </p>
      </>
    );
  }

  const totalReferrals = buddies.reduce((sum, b) => sum + b.referral_count, 0);

  return (
    <>
      <h1 style={{ fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>Buddies</h1>
      <p className="lede" style={{ marginTop: 18, maxWidth: "62ch" }}>
        External recruiters who forward job orders into your mailbox. {buddies.length}{" "}
        {buddies.length === 1 ? "buddy has" : "buddies have"} referred {totalReferrals}{" "}
        {totalReferrals === 1 ? "client" : "clients"}.
      </p>

      <div className="jo-controls" style={{ marginTop: 24 }}>
        <input
          className="jo-search"
          type="search"
          value={q}
          onChange={(event) => setQ(event.target.value)}
          placeholder="Search name or email…"
          aria-label="Search buddies"
        />
      </div>

      <div className="jo-controls" style={{ marginTop: 12 }}>
        <div className="jo-chips" role="group" aria-label="Filter referrals by period">
          {PERIODS.map((chip) => {
            const active = period === chip.key;
            return (
              <button
                key={chip.label}
                type="button"
                className="jo-chip"
                data-active={active ? "yes" : undefined}
                aria-pressed={active}
                onClick={() => setPeriod(chip.key)}
              >
                {chip.label}
              </button>
            );
          })}
        </div>
      </div>

      <nav className="jo-index" aria-label="Jump to buddies by first letter">
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

      <div style={{ marginTop: 16 }}>
        {buddies.length === 0 ? (
          <p className="body jo-note" aria-live="polite">
            No buddies match “{q}”.
          </p>
        ) : (
          <>
            <p className="body jo-note" aria-live="polite">
              Showing {buddies.length} {buddies.length === 1 ? "buddy" : "buddies"}.
            </p>
            <div className="card jo-table-card">
              <table className="jo-table" style={{ tableLayout: "auto" }}>
                <thead>
                  <tr>
                    <Th column={COLUMNS[0]} sort={sort} onSort={onSort} />
                    <Th column={COLUMNS[1]} sort={sort} onSort={onSort} />
                    {/* Mobile: a plain header, not a sort target. */}
                    <th className="row-k jo-th">Mobile</th>
                    <Th column={COLUMNS[2]} sort={sort} onSort={onSort} />
                    <Th column={COLUMNS[3]} sort={sort} onSort={onSort} />
                  </tr>
                </thead>
                <tbody>
                  {buddies.map((b) => (
                    <tr key={b.id} className="jo-row">
                      <td className="jo-td jo-td-strong">{b.name}</td>
                      <td className="jo-td" style={{ overflowWrap: "break-word", whiteSpace: "nowrap" }}>{b.email}</td>
                      <td className="jo-td">
                        <PhoneCell buddy={b} />
                      </td>
                      <td className="jo-td" style={{ whiteSpace: "nowrap" }}>
                        {b.email_domain ?? <span className="muted">—</span>}
                      </td>
                      <td className="jo-td" data-nowrap="yes">
                        {b.referral_count > 0 ? (
                          <button
                            type="button"
                            className="jo-rowbtn"
                            style={{ fontSize: "0.9375rem" }}
                            aria-label={`Show the ${b.referral_count} job orders referred by ${b.name}`}
                            onClick={() => setOpenBuddy(b)}
                          >
                            {b.referral_count}
                          </button>
                        ) : (
                          // A zero is not a link: there is nothing behind it.
                          <span className="muted">0</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {openBuddy && (
        <ReferralDialog
          buddy={openBuddy}
          period={period}
          onClose={() => setOpenBuddy(null)}
        />
      )}
    </>
  );
}

function Th({
  column,
  sort,
  onSort,
}: {
  column: { key: BuddySortKey; label: string };
  sort: BuddySort;
  onSort: (sort: BuddySort) => void;
}) {
  const active = sort.key === column.key;
  return (
    <th
      className="row-k jo-th"
      // The arrow is invisible to a screen reader, and a table that has
      // silently reordered itself is indistinguishable from one that lost rows.
      aria-sort={active ? (sort.descending ? "descending" : "ascending") : "none"}
      data-active={active ? "yes" : undefined}
    >
      <button
        type="button"
        className="jo-sort"
        // Re-clicking the sorted column reverses it; moving to a new column
        // starts ascending — except Referrals, where descending (most first)
        // is what anyone clicking a count column means. The same rule
        // `clients-table.tsx` applies to its Last seen column.
        onClick={() =>
          onSort(
            active
              ? { key: column.key, descending: !sort.descending }
              : { key: column.key, descending: column.key === "referral_count" },
          )
        }
      >
        <span>{column.label}</span>
        <span className="jo-arrow" aria-hidden="true">
          {active ? (sort.descending ? "↓" : "↑") : ""}
        </span>
      </button>
    </th>
  );
}

function PhoneCell({ buddy }: { buddy: Buddy }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(buddy.phone ?? "");
  const [saved, setSaved] = useState(buddy.phone ?? "");
  const [saving, setSaving] = useState(false);

  async function save() {
    const trimmed = value.trim();
    if (trimmed === saved) { setEditing(false); return; }
    setSaving(true);
    try {
      await fetch(`${BUDDIES_API_PATH}/${buddy.id}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone: trimmed || null }),
      });
      setSaved(trimmed || "");
      buddy.phone = trimmed || null;
      setEditing(false);
    } catch {
      /* best-effort */
    } finally {
      setSaving(false);
    }
  }

  if (editing) {
    return (
      <input
        type="tel"
        className="jo-search"
        style={{ padding: "4px 8px", fontSize: "0.8125rem", width: "100%", maxWidth: 140 }}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        disabled={saving}
        autoFocus
        onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setEditing(false); }}
        onBlur={save}
      />
    );
  }

  return (
    <button
      type="button"
      className="jo-rowbtn"
      style={{ fontSize: "0.9375rem" }}
      onClick={() => { setValue(saved); setEditing(true); }}
    >
      {saved || <span className="muted">Add</span>}
    </button>
  );
}

function ReferralDialog({
  buddy,
  period,
  onClose,
}: {
  buddy: Buddy;
  period: BuddyPeriod | null;
  onClose: () => void;
}) {
  const titleId = useId();
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; page: ReferralPage }
    | { status: "error" }
  >({ status: "loading" });

  // Fetched once per open. The `period` is the same window the count on the
  // row was scoped to, passed through so the list behind the number agrees
  // with the number the recruiter clicked — opening the modal under a "2
  // weeks" filter shows exactly the two weeks of job orders.
  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const params = new URLSearchParams();
        if (period) params.set("period", period);
        const qs = params.toString();
        const url = `${BUDDIES_API_PATH}/${buddy.id}/referrals${qs ? `?${qs}` : ""}`;
        const res = await fetch(url, {
          credentials: "include",
          signal: controller.signal,
        });
        if (!res.ok) throw new Error();
        const data = (await res.json()) as ReferralPage;
        setState({ status: "ready", page: data });
      } catch {
        if (!controller.signal.aborted) setState({ status: "error" });
      }
    })();
    return () => controller.abort();
    // `buddy.id` and `period` are the inputs; `buddy` is stable for an open
    // dialog (closing remounts it with a new buddy).
  }, [buddy.id, period]);

  return (
    <Dialog
      titleId={titleId}
      onClose={onClose}
      className="dlg-modal-wide"
      title={`Job orders referred by ${buddy.name}`}
    >
      {state.status === "loading" ? (
        <p className="body jo-note">Loading referrals.</p>
      ) : state.status === "error" ? (
        <p className="body jo-detail-error" role="alert">
          We could not load those referrals just now.
        </p>
      ) : state.page.items.length === 0 ? (
        <p className="body jo-note">
          {buddy.name} has not referred any job orders in this period.
        </p>
      ) : (
        <div className="card jo-table-card" style={{ marginTop: 0 }}>
          <table className="jo-table" style={{ tableLayout: "auto" }}>
            <thead>
              <tr>
                <th className="row-k jo-th">Position</th>
                <th className="row-k jo-th">Company</th>
                <th className="row-k jo-th">Received</th>
                <th className="row-k jo-th">Salary</th>
                <th className="row-k jo-th">Location</th>
              </tr>
            </thead>
            <tbody>
              {state.page.items.map((o) => (
                <tr key={o.id} className="jo-row">
                  <td className="jo-td jo-td-strong">
                    {o.job_title_raw ?? <span className="muted">—</span>}
                  </td>
                  <td className="jo-td">
                    {o.company_name_raw ?? <span className="muted">—</span>}
                  </td>
                  <td className="jo-td" data-nowrap="yes">
                    {day(o.received_datetime) ?? <span className="muted">—</span>}
                  </td>
                  <td className="jo-td">
                    {o.salary_raw ?? <span className="muted">—</span>}
                  </td>
                  <td className="jo-td">
                    {o.location_raw ?? <span className="muted">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Dialog>
  );
}
