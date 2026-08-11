"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  CLIENT_DISCOVERY_CREATE_PATH,
  CLIENT_DISCOVERY_PATH,
  CLIENT_DISCOVERY_POLL_MS,
  CLIENT_DISCOVERY_SCAN_PATH,
} from "../../api";

/**
 * The client auto-discovery panel: scan, watch, pick, create.
 *
 * The scan runs on the workers and this panel polls `GET /api/client-discovery`
 * while a run is `pending`/`running` — the sourcing panel's idiom, not SSE,
 * because a settings page open for the duration of one scan does not justify
 * a stream. Everything rendered comes from the run row; nothing here derives
 * a score or a ranking of its own.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the
 * page, not a list anything is matched against.
 */

type DiscoveredContact = {
  email: string;
  name: string;
  inbound: number;
  outbound: number;
  last_activity: string | null;
};

type DiscoveredDomain = {
  domain: string;
  score: number;
  received: number;
  sent: number;
  unique_contacts: number;
  last_activity: string | null;
  created: boolean;
  contacts: DiscoveredContact[];
};

type Run = {
  id: string;
  status: "pending" | "running" | "done" | "failed";
  lookback_days: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  inbox_scanned: number;
  sent_scanned: number;
  messages_truncated: boolean;
  domains_truncated: boolean;
  clients_enriched: number;
  contacts_added: number;
  error: string | null;
  results: DiscoveredDomain[] | null;
};

type Panel =
  | { status: "loading" }
  | { status: "unreadable"; message: string }
  | { status: "ready"; run: Run | null };

function isActive(run: Run | null): boolean {
  return run !== null && (run.status === "pending" || run.status === "running");
}

function day(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? "—" : new Date(ms).toLocaleDateString();
}

export function DiscoveryPanel() {
  const [panel, setPanel] = useState<Panel>({ status: "loading" });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [scanning, setScanning] = useState(false);
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [createdNote, setCreatedNote] = useState<string | null>(null);
  // A slow GET must not overwrite the state a newer action produced.
  const requestId = useRef(0);

  const load = useCallback(async () => {
    const myRequest = ++requestId.current;
    try {
      const res = await fetch(CLIENT_DISCOVERY_PATH, {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) {
        if (requestId.current === myRequest) {
          setPanel({
            status: "unreadable",
            message:
              res.status === 401
                ? "Your session has expired. Sign in again and this page will show your scan."
                : "The scan could not be read just now. Reload in a moment.",
          });
        }
        return;
      }
      const body = (await res.json()) as { run: Run | null };
      if (requestId.current === myRequest) {
        setPanel({ status: "ready", run: body.run });
      }
    } catch {
      if (requestId.current === myRequest) {
        setPanel({
          status: "unreadable",
          message: "We could not reach the server. Reload in a moment.",
        });
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll only while a run is actually working; a finished page costs nothing.
  const active = panel.status === "ready" && isActive(panel.run);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => void load(), CLIENT_DISCOVERY_POLL_MS);
    return () => clearInterval(timer);
  }, [active, load]);

  const startScan = useCallback(async () => {
    setScanning(true);
    setActionError(null);
    setCreatedNote(null);
    setSelected(new Set());
    try {
      const res = await fetch(CLIENT_DISCOVERY_SCAN_PATH, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!res.ok && res.status !== 409) {
        // 409 means a scan is already running — the reload below will show
        // it, which is better than an error about a state that is fine.
        const detail = (await res.json().catch(() => null)) as { detail?: string } | null;
        setActionError(
          detail?.detail ??
            "The scan could not be started just now. Try again in a moment.",
        );
      }
    } catch {
      setActionError("We could not reach the server. Try again in a moment.");
    } finally {
      setScanning(false);
      void load();
    }
  }, [load]);

  const createSelected = useCallback(async () => {
    if (selected.size === 0) return;
    setCreating(true);
    setActionError(null);
    setCreatedNote(null);
    try {
      const res = await fetch(CLIENT_DISCOVERY_CREATE_PATH, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ domains: Array.from(selected) }),
      });
      if (!res.ok) {
        const detail = (await res.json().catch(() => null)) as { detail?: string } | null;
        setActionError(
          detail?.detail ?? "Creating those clients failed. Try again in a moment.",
        );
        return;
      }
      const body = (await res.json()) as {
        results: { domain: string; outcome: string; contacts_added?: number }[];
      };
      const created = body.results.filter((r) => r.outcome === "created").length;
      const adopted = body.results.filter((r) => r.outcome === "already_existed").length;
      const contacts = body.results.reduce((n, r) => n + (r.contacts_added ?? 0), 0);
      setCreatedNote(
        `Added ${created} client${created === 1 ? "" : "s"}` +
          (adopted > 0 ? ` (${adopted} already existed)` : "") +
          (contacts > 0 ? ` and ${contacts} contact${contacts === 1 ? "" : "s"}.` : "."),
      );
      setSelected(new Set());
    } catch {
      setActionError("We could not reach the server. Try again in a moment.");
    } finally {
      setCreating(false);
      void load();
    }
  }, [selected, load]);

  const toggle = useCallback((domain: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(domain)) next.delete(domain);
      else next.add(domain);
      return next;
    });
  }, []);

  return (
    <section className="alt" style={{ padding: "56px 0" }}>
      <div className="wrap" aria-live="polite">
        <p className="body" style={{ marginTop: 0 }}>
          Reads who you have emailed with — sender names and addresses only, never
          message bodies — fills in contacts on the clients you already have, and
          lists the companies that are not clients yet so you can add them in one
          click.
        </p>

        {panel.status === "loading" && (
          <p className="lede" style={{ marginTop: 24, maxWidth: "none" }}>
            Checking for a scan.
          </p>
        )}

        {panel.status === "unreadable" && (
          <p className="body gl-error" style={{ marginTop: 24, maxWidth: "none" }} role="alert">
            {panel.message}
          </p>
        )}

        {panel.status === "ready" && (
          <>
            {isActive(panel.run) ? (
              <p className="lede" style={{ marginTop: 24, maxWidth: "none" }}>
                Scanning your mailbox — this reads only message headers and takes a
                minute or two on a busy mailbox. You can leave this page; the scan
                keeps going.
              </p>
            ) : (
              <div style={{ marginTop: 24 }}>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void startScan()}
                  disabled={scanning}
                >
                  {panel.run === null ? "Scan my mailbox" : "Scan again"}
                </button>
              </div>
            )}

            {actionError && (
              <p className="body gl-error" style={{ marginTop: 16, maxWidth: "none" }} role="alert">
                {actionError}
              </p>
            )}

            {panel.run?.status === "failed" && (
              <p className="body gl-error" style={{ marginTop: 16, maxWidth: "none" }} role="alert">
                {panel.run.error ?? "The last scan failed. Scan again to retry."}
              </p>
            )}

            {panel.run?.status === "done" && (
              <DoneRun
                run={panel.run}
                selected={selected}
                onToggle={toggle}
                onCreate={() => void createSelected()}
                creating={creating}
                createdNote={createdNote}
              />
            )}
          </>
        )}
      </div>
    </section>
  );
}

function DoneRun({
  run,
  selected,
  onToggle,
  onCreate,
  creating,
  createdNote,
}: {
  run: Run;
  selected: Set<string>;
  onToggle: (domain: string) => void;
  onCreate: () => void;
  creating: boolean;
  createdNote: string | null;
}) {
  const results = run.results ?? [];
  const open = results.filter((r) => !r.created);

  return (
    <div style={{ marginTop: 24 }}>
      <p className="body muted">
        Read {run.inbox_scanned + run.sent_scanned} message header
        {run.inbox_scanned + run.sent_scanned === 1 ? "" : "s"} from the last{" "}
        {run.lookback_days} days
        {run.messages_truncated ? " (stopped at the scan limit)" : ""}.{" "}
        {run.contacts_added > 0
          ? `Filled in ${run.contacts_added} contact${
              run.contacts_added === 1 ? "" : "s"
            } across ${run.clients_enriched} existing client${
              run.clients_enriched === 1 ? "" : "s"
            }.`
          : "Your existing clients already had every contact this scan found."}
      </p>

      {createdNote && (
        <p className="body" style={{ marginTop: 12 }} role="status">
          {createdNote}
        </p>
      )}

      {results.length === 0 ? (
        <p className="body muted" style={{ marginTop: 16 }}>
          No new companies found — every business domain in this window is already
          a client.
        </p>
      ) : (
        <>
          {run.domains_truncated && (
            <p className="body muted" style={{ marginTop: 12 }}>
              Showing the highest-ranked companies; the scan found more than this
              list holds.
            </p>
          )}
          <ul className="gl-list" style={{ marginTop: 16 }}>
            {results.map((entry) => (
              <li key={entry.domain} className="card gl-row">
                <label
                  style={{ display: "flex", gap: 12, alignItems: "baseline", cursor: "pointer" }}
                >
                  <input
                    type="checkbox"
                    checked={entry.created || selected.has(entry.domain)}
                    disabled={entry.created}
                    onChange={() => onToggle(entry.domain)}
                    aria-label={`Add ${entry.domain} as a client`}
                  />
                  <span className="gl-code">{entry.domain}</span>
                  <span className="gl-meaning">
                    {entry.contacts[0]
                      ? `${entry.contacts[0].name} · ${entry.contacts[0].email}`
                      : "No named contact"}
                  </span>
                  {entry.created && <span className="gl-origin">Added</span>}
                </label>
                <div className="rows gl-row-rows">
                  <div className="row">
                    <span className="row-k">Received / sent</span>
                    <span>
                      {entry.received} / {entry.sent}
                    </span>
                  </div>
                  <div className="row">
                    <span className="row-k">People</span>
                    <span>{entry.unique_contacts}</span>
                  </div>
                  <div className="row">
                    <span className="row-k">Last activity</span>
                    <span>{day(entry.last_activity)}</span>
                  </div>
                  <div className="row">
                    <span className="row-k">Score</span>
                    <span>{entry.score}</span>
                  </div>
                </div>
              </li>
            ))}
          </ul>
          {open.length > 0 && (
            <button
              type="button"
              className="btn btn-primary"
              style={{ marginTop: 16 }}
              onClick={onCreate}
              disabled={creating || selected.size === 0}
            >
              {creating
                ? "Adding…"
                : selected.size === 1
                  ? "Add 1 selected as client"
                  : `Add ${selected.size} selected as clients`}
            </button>
          )}
        </>
      )}
    </div>
  );
}
