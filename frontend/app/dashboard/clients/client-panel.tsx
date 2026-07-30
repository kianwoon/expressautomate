"use client";

import { useEffect, useRef, useState } from "react";

import { CLIENTS_PATH } from "../../api";
import type { Client, ClientMention, ClientPage, Contact, MatchedBy } from "../clients";
import { getClient, mergeClient, suspendClient, unmergeClient, unsuspendClient } from "../clients";
import { day, when } from "../format";
import { ClientForm } from "./client-form";
import { ClientLogo } from "./client-logo";

/**
 * One client in full, beside the list.
 *
 * A client-specific sibling of `candidate-panel.tsx`. There is still no
 * delete — clients are archived, never destroyed — and no owner-only action:
 * every button here is open to any signed-in user.
 *
 * Editing lives in `client-form.tsx` rather than in this file. That is a size
 * decision as much as a structural one: this panel was already past 450 lines
 * before the commercial fields existed, and a twenty-control form inside it
 * would have made the one file nobody wants to open.
 *
 * Suspend and Unsuspend call the API from here rather than being threaded
 * through the page as `onConfirm`/`onArchive` are, following `MergePicker`
 * directly below: suspending carries a reason typed in this panel, and
 * passing that reason up through a prop only to have it come straight back
 * would put the panel's own input in the parent's signature.
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
  suspended: "Suspended",
  archived: "Archived",
  merged: "Merged",
};

const MATCHED_BY_LABEL: Record<MatchedBy, string> = {
  email_domain: "Mail arrived from this company's domain",
  name: "The company's name only resembled something in the email",
};

export function ClientPanel({
  row,
  onConfirm,
  onArchive,
  onRestore,
  onChanged,
  onSelectClient,
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
  /** Selects another client by id in the parent's detail pane — the panel's
   *  only navigation hook, used to jump to a merge survivor. */
  onSelectClient: (id: string) => void;
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
      onSelectClient={onSelectClient}
    />
  );
}

function Detail({
  row,
  onConfirm,
  onArchive,
  onRestore,
  onChanged,
  onSelectClient,
}: {
  row: Client;
  onConfirm: () => Promise<void>;
  onArchive: () => Promise<void>;
  onRestore: () => Promise<void>;
  onChanged: () => void;
  onSelectClient: (id: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [askingReason, setAskingReason] = useState(false);
  const [reason, setReason] = useState("");

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

  async function suspend() {
    await run(async () => {
      // Trimmed to nothing means nothing was said, and nothing is what gets
      // sent — an invented reason is worse than a blank one (§15).
      const typed = reason.trim();
      await suspendClient(row.id, typed === "" ? undefined : typed);
      setAskingReason(false);
      setReason("");
      onChanged();
    }, "We could not suspend that client just now.");
  }

  async function unsuspend() {
    await run(async () => {
      await unsuspendClient(row.id);
      onChanged();
    }, "We could not take that client off hold just now.");
  }

  return (
    <aside className="card jo-detail" aria-label="Client details">
      <div className="jo-detail-head">
        <span className="eyebrow">Details</span>
        <span className="eyebrow">{STATUS_LABEL[row.status]}</span>
      </div>

      <div className="jo-detail-avatar">
        <ClientLogo client={row} />
        <h3 className="jo-detail-title">{row.name}</h3>
      </div>

      {/* Above everything, including the name's own details: a suspended
          client refuses submissions, and someone reading this panel to decide
          whether to submit needs to know that before they read anything
          else. */}
      {row.status === "suspended" && <SuspensionBanner row={row} />}

      {editing && (
        <ClientForm
          client={row}
          onCancel={() => setEditing(false)}
          onDone={() => {
            setEditing(false);
            onChanged();
          }}
        />
      )}

      {row.status === "merged" ? (
        <MergedInto row={row} onUnmerge={unmerge} busy={busy} onSelectClient={onSelectClient} />
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

          <ClientFacts row={row} onEdit={() => setEditing(true)} />

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
          {/* Each offered only for the status that accepts it — the server
              refuses the others with a 400, and a button whose only outcome
              is an error message is not an offer. Suspend is `confirmed`
              only; unconfirmed means nobody has agreed this client is real
              yet, so there is nothing to put on hold. */}
          {row.status === "confirmed" && !askingReason && (
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={() => setAskingReason(true)}
            >
              Suspend…
            </button>
          )}
          {row.status === "suspended" && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() => void unsuspend()}
            >
              {busy ? "Saving…" : "Unsuspend"}
            </button>
          )}
          {row.status !== "merged" && (
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={() => setEditing(true)}
            >
              Edit
            </button>
          )}
        </div>

        {askingReason && (
          <div className="cl-suspend-ask">
            <label className="body" style={{ display: "grid", gap: 4 }}>
              <span className="row-k">Why is this client on hold? (optional)</span>
              <input
                className="jo-search"
                autoFocus
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Unpaid invoice, contract under review…"
              />
            </label>
            <p className="body jo-sub">
              Whatever you type here is what the recruiter trying to submit a candidate will be
              shown when the submission is refused. Leave it blank if there is nothing to say.
            </p>
            <div style={{ display: "flex", gap: 10 }}>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy}
                onClick={() => void suspend()}
              >
                {busy ? "Saving…" : "Suspend client"}
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy}
                onClick={() => {
                  setAskingReason(false);
                  setReason("");
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </aside>
  );
}

/** Why this client is on hold, and since when.
 *
 *  The reason is optional on the server, so it is genuinely absent much of
 *  the time and the banner says so plainly rather than inventing one (§15).
 *  The date is the half a recruiter can always be given: "on hold since
 *  March" is actionable even when nobody wrote down why. */
function SuspensionBanner({ row }: { row: Client }) {
  return (
    <div className="cl-suspended-banner" role="status">
      <span className="row-k">On hold</span>
      <p className="body">
        {row.suspended_reason ?? (
          <span className="muted">No reason was recorded when this client was suspended.</span>
        )}
      </p>
      <p className="body jo-sub">
        {row.suspended_at ? `Suspended ${day(row.suspended_at)}.` : "Suspended."} Candidates cannot
        be submitted to this client until it is taken off hold.
      </p>
    </div>
  );
}

/** The facts a recruiter owns, read-only, with the one button that changes
 *  them. Read-only on purpose: this panel is where a client is judged and
 *  acted on, and an always-live form here would put a stray keystroke between
 *  a recruiter and the record. Editing is a deliberate act, in a dialog.
 *
 *  Nothing is filled in on a recruiter's behalf. An unset fee reads "Not
 *  recorded", not "0%" — a fee of zero is a commercial fact somebody agreed,
 *  and claiming it because a column is null would be exactly the fabrication
 *  §15 forbids. */
function ClientFacts({ row, onEdit }: { row: Client; onEdit: () => void }) {
  return (
    <div className="jo-detail-prose">
      <div className="cl-facts-head">
        <span className="row-k">Details</span>
        <button type="button" className="btn btn-secondary cl-edit" onClick={onEdit}>
          Edit
        </button>
      </div>
      <div className="rows jo-detail-rows">
        <Fact label="Website" value={row.website} />
        <Fact label="Phone" value={row.phone} />
        <Fact label="Address" value={row.address} />
        <Fact
          label="Fee"
          value={row.fee_percent != null ? `${row.fee_percent}%` : null}
        />
        <Fact
          label="Payment terms"
          value={row.payment_terms_days != null ? `${row.payment_terms_days} days` : null}
        />
        <Fact label="Notes" value={row.notes} />
      </div>
      <Contacts contacts={row.contacts} />
    </div>
  );
}

const NOT_RECORDED = "Not recorded";

function Fact({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="row">
      <span className="row-k">{label}</span>
      <span className={value ? undefined : "muted"}>{value ?? NOT_RECORDED}</span>
    </div>
  );
}

/** The people at this company. The primary one is marked rather than merely
 *  sorted first: the API already returns primary-first, and a list whose
 *  order carries the meaning loses it the moment anyone reads it out of
 *  order or a screen reader announces it flat. */
function Contacts({ contacts }: { contacts: Contact[] | undefined }) {
  if (!contacts) return null;
  return (
    <div style={{ marginTop: 12 }}>
      <span className="row-k">Contacts ({contacts.length})</span>
      {contacts.length === 0 ? (
        <p className="body muted">No contacts recorded.</p>
      ) : (
        <ul className="cl-contacts">
          {contacts.map((c) => (
            <li key={c.id} className="cl-contact">
              <span className="cl-contact-name">
                {c.name}
                {c.is_primary && <span className="cl-primary-mark"> Primary</span>}
              </span>
              {c.title && <span className="muted"> — {c.title}</span>}
              <span className="body jo-sub">
                {[c.email, c.phone].filter(Boolean).join(" · ") || NOT_RECORDED}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
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

/** Names the merge survivor instead of showing its bare id, and makes it
 *  navigable to that client's record — a UUID told a recruiter nothing
 *  without going to curl for it. */
function MergedInto({
  row,
  onUnmerge,
  busy,
  onSelectClient,
}: {
  row: Client;
  onUnmerge: () => void;
  busy: boolean;
  onSelectClient: (id: string) => void;
}) {
  const [survivor, setSurvivor] = useState<Client | null>(null);
  const survivorId = row.merged_into_client_id;

  useEffect(() => {
    if (!survivorId) {
      setSurvivor(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const client = await getClient(survivorId);
        if (!cancelled) setSurvivor(client);
      } catch {
        /* the id still renders below; not worth an error banner here */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [survivorId]);

  return (
    <div className="jo-detail-rows rows">
      <p className="body">
        This record was merged into{" "}
        {survivorId ? (
          <button
            type="button"
            className="jo-link"
            onClick={() => onSelectClient(survivorId)}
            style={{
              background: "none",
              border: "none",
              padding: 0,
              font: "inherit",
              textDecoration: "underline",
              cursor: "pointer",
            }}
          >
            {survivor ? survivor.name : survivorId}
          </button>
        ) : (
          "another client"
        )}
        . Their record now holds every mention this one had that could be moved.
      </p>
      <button type="button" className="btn btn-secondary" onClick={onUnmerge} disabled={busy}>
        {busy ? "Working…" : "Unmerge"}
      </button>
    </div>
  );
}

// One fetch is `limit=200` (`CLIENTS_PAGE_LIMIT`, the server's own ceiling).
// A single page silently truncated the corpus for any tenant past 200
// clients: a duplicate last seen months ago would sort past the first page
// and simply not appear, with nothing telling the recruiter a cut happened.
// So this pages until `total` is exhausted instead of trusting one page —
// and if a corpus is ever large enough to hit `MERGE_PICKER_MAX_PAGES`, the
// cap is disclosed in the picker rather than repeating the same silent
// truncation at a higher number.
const MERGE_PICKER_MAX_PAGES = 25; // 25 * 200 = 5,000 clients before disclosure

/** Opens a search over the client list and merges the current client into
 *  whichever result is chosen. The only way into `POST /clients/{id}/merge`
 *  — without it a duplicate proposal is unreachable except by curl.
 *
 * `GET /clients` has no `q` parameter (unlike candidates), so this fetches
 * every non-merged client, paging until the server's own `total` is
 * exhausted, and filters by name on the client side rather than asking the
 * server to search. */
function MergePicker({ clientId, onMerged }: { clientId: string; onMerged: () => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [all, setAll] = useState<Client[]>([]);
  const [loadingAll, setLoadingAll] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoadingAll(true);
    setTruncated(false);
    (async () => {
      try {
        const collected: Client[] = [];
        let offset = 0;
        let total = Infinity;
        let page = 0;
        while (offset < total && page < MERGE_PICKER_MAX_PAGES) {
          const params = new URLSearchParams({ limit: "200", offset: String(offset) });
          const res = await fetch(`${CLIENTS_PATH}?${params.toString()}`, {
            credentials: "include",
            headers: { Accept: "application/json" },
            signal: controller.signal,
          });
          if (!res.ok) break;
          const body = (await res.json()) as ClientPage;
          collected.push(...body.items);
          total = body.total;
          offset += body.items.length;
          page += 1;
          // A page that returned nothing (e.g. limit misbehaving) would spin
          // forever otherwise.
          if (body.items.length === 0) break;
        }
        if (!controller.signal.aborted) {
          setAll(collected.filter((c) => c.id !== clientId));
          setTruncated(offset < total);
        }
      } catch {
        /* the box will simply show nothing; not worth an error banner */
      } finally {
        if (!controller.signal.aborted) setLoadingAll(false);
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
      {loadingAll && (
        <p className="body muted" style={{ marginTop: 8 }}>
          Loading every client…
        </p>
      )}
      {!loadingAll && truncated && (
        <p className="body jo-detail-error" role="alert" style={{ marginTop: 8 }}>
          There are more clients than this search could load. Some results may be missing —
          narrow your search or merge from a filtered list instead.
        </p>
      )}
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
                {/* Status is shown rather than excluding archived targets: an
                 *  archived client can legitimately be the right merge target
                 *  (e.g. re-merging a duplicate that was archived by
                 *  mistake), and hiding it would just move this same "invisible
                 *  survivor" defect one level up. Showing the status lets the
                 *  recruiter make an informed choice instead. */}
                <span className="muted"> — {STATUS_LABEL[c.status]}</span>
                {c.email_domain && <span className="muted"> · {c.email_domain}</span>}
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
