"use client";

import { useEffect, useRef, useState } from "react";

import { CLIENTS_PATH } from "../../api";
import type { Client, ClientMention, ClientPage, MatchedBy } from "../clients";
import { mergeClient, unmergeClient } from "../clients";
import { day, when } from "../format";

/**
 * One client in full, beside the list.
 *
 * A client-specific sibling of `candidate-panel.tsx`. The differences are
 * deliberate, not omissions: there is no edit form (the pipeline proposes a
 * client, a human only judges it) and no delete (clients have no owner-only
 * action at all — every button here is open to any signed-in user).
 *
 * The mentions list is the point of this panel. Confirming "is this really a
 * client?" is a judgement about evidence, and `matched_by` is the only thing
 * that tells a recruiter how strong that evidence is: a domain match is a
 * fact about where the mail came from, a name match is only a resemblance
 * the matcher noticed, and telling those two apart in words is exactly what
 * the recruiter is being asked to do.
 */

const STATUS_LABEL: Record<Client["status"], string> = {
  unconfirmed: "Unconfirmed",
  confirmed: "Confirmed",
  archived: "Archived",
  merged: "Merged",
};

const MATCHED_BY_LABEL: Record<MatchedBy, string> = {
  email_domain: "Mail arrived from this company's domain",
  name: "The company's name only resembled something in the email",
  human: "A person matched this by hand",
};

export function ClientPanel({
  row,
  onConfirm,
  onArchive,
  onRestore,
  onChanged,
}: {
  row: Client | null;
  onConfirm: () => Promise<void>;
  onArchive: () => Promise<void>;
  /** Undoes an archive. Archiving is reversible by design, so this is offered
   *  wherever Archive is, just on the other side of the same toggle. */
  onRestore: () => Promise<void>;
  /** Called after a confirm, archive, restore, merge or unmerge succeeds, so
   *  the caller can refetch the list and the detail record. */
  onChanged: () => void;
}) {
  if (!row) {
    return (
      <aside className="card jo-detail" aria-label="Client details">
        <span className="eyebrow">Details</span>
        <p className="body jo-detail-empty">
          Select a client to see the evidence behind it — which emails mentioned it and how each
          one was matched.
        </p>
      </aside>
    );
  }

  return (
    <Detail
      key={row.id}
      row={row}
      onConfirm={onConfirm}
      onArchive={onArchive}
      onRestore={onRestore}
      onChanged={onChanged}
    />
  );
}

function Detail({
  row,
  onConfirm,
  onArchive,
  onRestore,
  onChanged,
}: {
  row: Client;
  onConfirm: () => Promise<void>;
  onArchive: () => Promise<void>;
  onRestore: () => Promise<void>;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<void>, failMessage: string) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : failMessage);
    } finally {
      setBusy(false);
    }
  }

  async function unmerge() {
    await run(async () => {
      await unmergeClient(row.id);
      onChanged();
    }, "We could not unmerge that just now.");
  }

  return (
    <aside className="card jo-detail" aria-label="Client details">
      <div className="jo-detail-head">
        <span className="eyebrow">Details</span>
        <span className="eyebrow">{STATUS_LABEL[row.status]}</span>
      </div>

      <h3 className="jo-detail-title">{row.name}</h3>

      {row.status === "merged" ? (
        <MergedInto row={row} onUnmerge={unmerge} busy={busy} />
      ) : (
        <>
          <div className="rows jo-detail-rows">
            <div className="row">
              <span className="row-k">Mail domain</span>
              <span className={row.email_domain ? undefined : "muted"}>
                {row.email_domain ?? "Not mentioned"}
              </span>
            </div>
            <div className="row">
              <span className="row-k">Last seen</span>
              <span className={row.last_seen_at ? undefined : "muted"}>
                {day(row.last_seen_at) ?? "Never"}
              </span>
            </div>
          </div>

          <Mentions mentions={row.mentions} />

          <MergePicker clientId={row.id} onMerged={onChanged} />
        </>
      )}

      <div className="jo-detail-actions">
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          {row.status === "unconfirmed" && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() => run(onConfirm, "We could not save that just now.")}
            >
              {busy ? "Saving…" : "Confirm"}
            </button>
          )}
          {row.status !== "archived" && row.status !== "merged" && (
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={() => run(onArchive, "We could not save that just now.")}
            >
              {busy ? "Saving…" : "Archive"}
            </button>
          )}
          {row.status === "archived" && (
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={() => run(onRestore, "We could not save that just now.")}
            >
              {busy ? "Saving…" : "Restore"}
            </button>
          )}
        </div>
      </div>

      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </aside>
  );
}

/** The evidence trail. Newest first, matching what the API returns. */
function Mentions({ mentions }: { mentions: ClientMention[] | undefined }) {
  if (!mentions) return null;
  return (
    <div className="jo-detail-prose">
      <span className="row-k">Mentions ({mentions.length})</span>
      {mentions.length === 0 ? (
        <p className="body muted">No emails have mentioned this client.</p>
      ) : (
        <ul style={{ marginTop: 8, listStyle: "none", padding: 0 }}>
          {mentions.map((m) => (
            <li key={m.id} className="row" style={{ alignItems: "flex-start" }}>
              <span className="row-k">{when(m.created_at)}</span>
              <span>
                {MATCHED_BY_LABEL[m.matched_by]}
                {!m.email_message_id && (
                  <span className="muted"> — the source email has since been purged</span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function MergedInto({
  row,
  onUnmerge,
  busy,
}: {
  row: Client;
  onUnmerge: () => void;
  busy: boolean;
}) {
  return (
    <div className="jo-detail-rows rows">
      <p className="body">
        This record was merged into another client
        {row.merged_into_client_id && (
          <>
            {" "}
            (id <code>{row.merged_into_client_id}</code>)
          </>
        )}
        . Their record now holds every mention this one had that could be moved.
      </p>
      <button type="button" className="btn btn-secondary" onClick={onUnmerge} disabled={busy}>
        {busy ? "Working…" : "Unmerge"}
      </button>
    </div>
  );
}

/** Opens a search over the client list and merges the current client into
 *  whichever result is chosen. The only way into `POST /clients/{id}/merge`
 *  — without it a duplicate proposal is unreachable except by curl.
 *
 * `GET /clients` has no `q` parameter (unlike candidates), so this fetches
 * the live, non-merged page and filters by name on the client side rather
 * than asking the server to search. */
function MergePicker({ clientId, onMerged }: { clientId: string; onMerged: () => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [all, setAll] = useState<Client[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    (async () => {
      try {
        const params = new URLSearchParams({ limit: "200" });
        const res = await fetch(`${CLIENTS_PATH}?${params.toString()}`, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) return;
        const body = (await res.json()) as ClientPage;
        setAll(body.items.filter((c) => c.id !== clientId));
      } catch {
        /* the box will simply show nothing; not worth an error banner */
      }
    })();
    return () => controller.abort();
  }, [open, clientId]);

  const needle = q.trim().toLowerCase();
  const results = needle ? all.filter((c) => c.name.toLowerCase().includes(needle)) : [];

  async function choose(targetId: string) {
    setBusy(true);
    setError(null);
    try {
      await mergeClient(clientId, targetId);
      setOpen(false);
      setQ("");
      onMerged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not merge that just now.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div className="jo-detail-prose">
        <button type="button" className="btn btn-secondary" onClick={() => setOpen(true)}>
          Merge into…
        </button>
      </div>
    );
  }

  return (
    <div className="jo-detail-prose">
      <span className="row-k">Merge into</span>
      <input
        className="jo-search"
        type="search"
        autoFocus
        value={q}
        onChange={(event) => setQ(event.target.value)}
        placeholder="Search by name…"
        aria-label="Search for the client to merge into"
        style={{ marginTop: 8 }}
      />
      {results.length > 0 && (
        <ul style={{ marginTop: 8, listStyle: "none", padding: 0 }}>
          {results.map((c) => (
            <li key={c.id} style={{ marginTop: 4 }}>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => choose(c.id)}
                style={{ width: "100%", textAlign: "left" }}
              >
                {c.name}
                {c.email_domain && <span className="muted"> — {c.email_domain}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
        <button type="button" className="btn btn-secondary" onClick={() => setOpen(false)} disabled={busy}>
          Cancel
        </button>
      </div>
      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
