"use client";

import { MAILBOX_ACTIVITY_PATH } from "../api";
import type { Me } from "../auth";
import { plural, when } from "./format";
import { useActivity } from "./opportunities";

/**
 * What ingestion has been doing, and what state the mailbox is in.
 *
 * These sit together because they answer one question between them: is this
 * thing working? The counts elsewhere on the page say how much arrived, which
 * looks identical whether the last sync succeeded an hour ago or has been
 * failing all morning against a stale total.
 *
 * A failed event is shown as a failed event. Filtering the failures out would
 * make the panel useless precisely when it matters — and the failures are the
 * only reason to have it.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export function SyncActivity() {
  const state = useActivity(MAILBOX_ACTIVITY_PATH);

  return (
    <div className="card jo-activity">
      <h3>Recent sync activity</h3>

      {state.status === "loading" ? (
        <p className="body jo-sub">Loading recent activity.</p>
      ) : state.status === "unreadable" ? (
        // Never "no activity". A fetch that failed and a mailbox that has done
        // nothing are different facts, and only one of them is about the user.
        <p className="body jo-sub">{state.message}</p>
      ) : state.events.length === 0 ? (
        <p className="body jo-sub">
          Nothing has happened yet. Events appear here as mail is fetched and read.
        </p>
      ) : (
        <ul className="jo-events">
          {state.events.map((event, index) => (
            <li key={`${event.at}-${index}`} data-outcome={event.outcome}>
              <span className="jo-event-dot" aria-hidden="true" />
              <span className="jo-event-body">
                <strong>{humanise(event.kind)}</strong>
                <span className="jo-sub">
                  {event.detail?.trim() ? (
                    event.detail
                  ) : (
                    <span className="muted">No detail recorded</span>
                  )}
                </span>
              </span>
              <span className="jo-event-when">{when(event.at) ?? "Unknown time"}</span>
              {/* The outcome is carried by a colour, and colour alone is not a
                  message. A failure says "failed" in words too. */}
              {event.outcome === "failed" && <span className="jo-event-tag">Failed</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * `delta_sync_failed` → "Delta sync failed".
 *
 * The kinds come from the backend and this page cannot know the full set, so
 * this reformats rather than translating from a table. A kind we have never
 * seen still renders as readable words instead of falling through to a raw
 * identifier or, worse, to nothing.
 */
function humanise(kind: string): string {
  const words = kind.replace(/[_-]+/g, " ").trim();
  if (!words) return "Activity";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The mailbox itself: the grant, the state, the window that has been read. */
export function MailboxOverview({ me }: { me: Me }) {
  const { total, in_progress: inProgress, awaiting_extraction: awaiting } = me.mailbox.ingested;
  const waiting = inProgress + awaiting;

  return (
    <div className="card jo-overview">
      <h3>Mailbox overview</h3>
      <div className="rows jo-overview-rows">
        <Row k="Permission" v={me.mailbox.connected ? "Granted" : "Not granted"} />
        <Row k="State" v={stateLabel(me)} />
        <Row k="Emails read" v={plural(total, "email")} />
        <Row
          k="Still in flight"
          v={
            waiting === 0
              ? "Nothing waiting"
              : inProgress > 0
                ? `${inProgress.toLocaleString()} fetching, ${awaiting.toLocaleString()} awaiting extraction`
                : `${awaiting.toLocaleString()} awaiting extraction`
          }
        />
        <Row k="Oldest email" v={day(me.mailbox.oldest_received)} empty="Nothing read yet" />
        <Row k="Newest email" v={day(me.mailbox.newest_received)} empty="Nothing read yet" />
      </div>
      <p className="body jo-sub">Read-only. We never send, reply to, or delete anything.</p>
    </div>
  );
}

/**
 * `empty` is per-row rather than one shared fallback. "Not mentioned" is
 * extraction vocabulary — it means the AI read an email and the field was not
 * in it (§15). Reusing it for a mailbox that has simply never done anything
 * says something quite different and slightly wrong.
 */
function Row({ k, v, empty = "Nothing yet" }: { k: string; v: string | null; empty?: string }) {
  return (
    <div className="row">
      <span className="row-k">{k}</span>
      <span className={v ? undefined : "muted"}>{v ?? empty}</span>
    </div>
  );
}

function stateLabel(me: Me): string {
  if (me.mailbox.awaiting_period) return "Awaiting your choice";
  if (me.mailbox.status === null) return "No mailbox connected";
  if (me.mailbox.status === "needs_reauth") return "Needs reconnecting";
  if (me.mailbox.status === "disconnected") return "Disconnected";
  return me.mailbox.ingestion_active ? "Reading new mail" : "Starting up";
}

function day(iso: string | null): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
