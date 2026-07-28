"use client";

import { useEffect, useRef, useState } from "react";

import { EVENTS_PATH } from "./api";

/**
 * The dashboard's live wire: one stream, shared, that says when to look again.
 *
 * The backend knows the exact moment a mailbox changed — Graph tells it — so the
 * browser is told rather than made to ask. What arrives is a nudge and nothing
 * more: a kind, no data. Every actual read still goes through the ordinary
 * endpoints, which is what keeps one tenant's rows unreachable from another's
 * session; a stream that carried the rows themselves would be a second data
 * path to get tenant scoping wrong in.
 *
 * allow-hardcode: the three kinds below are a wire protocol shared with
 * `backend/app/api/events.py`, not configuration. A setting that had to match
 * the other end of a socket would be a setting that could only ever be wrong.
 */
export type EventKind = "mail" | "extraction" | "mailbox";

/**
 * Why a listener is being called.
 *
 * `open` is not a change — it is the stream having just connected, which matters
 * because connections drop: a laptop suspends, a deploy restarts the service,
 * a network moves. Anything published during the gap went to nobody, since Redis
 * pub/sub does not hold messages for absent subscribers. So a connection has to
 * be treated as "you have missed an unknown amount" and answered with a refetch.
 * Without this the stream is silently lossy in exactly the situation where it is
 * most likely to have lost something.
 */
export type Nudge = EventKind | "open";

type Listener = (nudge: Nudge) => void;

/**
 * One connection for the whole page, not one per hook.
 *
 * Three hooks want these events. Three `EventSource`s would mean three
 * long-lived requests per open dashboard — and over HTTP/1.1 a browser allows
 * six per origin, so three of them are half the page's budget spent on
 * listening rather than fetching. The stream is opened when the first listener
 * arrives and closed when the last one leaves.
 */
let source: EventSource | null = null;
const listeners = new Set<Listener>();

/** The window a reconnect's catch-up refetch is scattered across. See `open`. */
const OPEN_SPREAD_MS = 2000;

/**
 * How long a burst of nudges is gathered before the listeners are told.
 *
 * Ingestion does not arrive one tidy row at a time. A first backfill stores
 * emails and extractions in a stream, and each one publishes — so without this,
 * every stored row costs the browser a refetch of the table, the counts and the
 * activity log, sustained, at exactly the moment a new user is watching. That is
 * more traffic than the poll this replaced, which is the one outcome that would
 * make the change pointless. Collapsed into one refresh per window per kind, a
 * backfill costs a bounded trickle and stops the instant it is done — where a
 * poll went on forever regardless.
 */
const COALESCE_MS = 2000;

/** Reconnect backoff after the stream is failed outright. See the error handler. */
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30_000;

/**
 * Whether the page is actually being told things — the fact behind the status
 * light on the dashboard.
 *
 * `waiting` covers both the first connection and every reconnection, because to
 * a reader they are the same thing: not live yet, no reason to worry. `offline`
 * is the one that means something is wrong, and it is deliberately slow to
 * appear — see `OFFLINE_GRACE_MS`.
 */
export type LiveStatus = "waiting" | "live" | "offline";

/**
 * How long the stream must be down before the page admits it.
 *
 * A dropped connection is usually a deploy or a sleeping laptop, and it is back
 * within a second or two. Without a grace period the light would flash a
 * warning at everybody, several times a week, for something that had already
 * fixed itself before they finished reading it — and a warning that is usually
 * wrong is one people learn to ignore, which costs exactly the times it is
 * right. Cancelled the moment the stream reconnects.
 */
const OFFLINE_GRACE_MS = 4000;

let status: LiveStatus = "waiting";
let offlineTimer: ReturnType<typeof setTimeout> | null = null;
const watchers = new Set<(status: LiveStatus) => void>();

function tell(next: LiveStatus): void {
  if (status === next) return;
  status = next;
  for (const watcher of [...watchers]) watcher(next);
}

function setStatus(next: LiveStatus): void {
  // ONLY a working connection cancels a pending outage. Cancelling on
  // "waiting" too would restart the grace period on every error event, and a
  // stream failing in a loop emits one of those per attempt — so the countdown
  // would be reset forever by the very failures it is timing, and the light
  // would sit on "connecting" through an outage of any length.
  if (next === "live" && offlineTimer !== null) {
    clearTimeout(offlineTimer);
    offlineTimer = null;
  }
  // Once the outage is declared, only a working connection lifts it. A failing
  // stream emits an error per attempt and each attempt calls `open`, so without
  // this latch the light flips back to "Connecting" every few seconds for the
  // whole outage — reading as though it were about to recover, forever, and
  // re-announcing itself to a screen reader on every flip. The first version of
  // this guard protected the countdown but not the verdict, which moved the bug
  // rather than fixing it.
  if (status === "offline" && next !== "live") return;
  tell(next);
}

/** Report an outage, but only if it is still true in a few seconds' time. */
function setOfflineSoon(): void {
  if (offlineTimer !== null || status === "offline") return;
  offlineTimer = setTimeout(() => {
    offlineTimer = null;
    tell("offline");
  }, OFFLINE_GRACE_MS);
}

let pending: Set<Nudge> = new Set();
let coalescing: ReturnType<typeof setTimeout> | null = null;

function announce(nudge: Nudge): void {
  pending.add(nudge);
  // Already gathering — this nudge joins the batch rather than starting a
  // second timer. Deliberately not a plain debounce: a sustained backfill would
  // reset a debounce on every row and so never deliver anything at all, which
  // is worse than delivering late.
  if (coalescing !== null) return;

  coalescing = setTimeout(() => {
    coalescing = null;
    const batch = [...pending];
    pending = new Set();
    // Both copied before iterating: a listener is free to unsubscribe in
    // response to what it hears, and mutating the set mid-iteration would skip
    // its neighbour.
    for (const listener of [...listeners]) {
      for (const one of batch) listener(one);
    }
  }, COALESCE_MS);
}

let retryIn = RETRY_BASE_MS;
let retrying: ReturnType<typeof setTimeout> | null = null;

function open(): void {
  if (source) return;

  // withCredentials so the session cookie is sent when the API is a separate
  // origin in local development. Same-origin in production, where it is moot.
  const stream = new EventSource(EVENTS_PATH, { withCredentials: true });
  source = stream;
  setStatus("waiting");

  // Spread over a couple of seconds rather than answered at once. A deploy drops
  // every open stream simultaneously, and `EventSource` brings them all back
  // within a moment of each other — so an immediate catch-up refetch means every
  // dashboard in every agency hits the API in the same instant the service is
  // coldest. The user waits up to two seconds longer for data they have not been
  // told about yet, which nobody can perceive.
  stream.addEventListener("open", () => {
    // A connection that worked earns a fresh budget: an hour-old backoff of
    // thirty seconds should not still be in force after a deploy has come and
    // gone and the stream has been healthy since.
    retryIn = RETRY_BASE_MS;
    setStatus("live");
    setTimeout(() => announce("open"), Math.random() * OPEN_SPREAD_MS);
  });

  stream.addEventListener("message", (event: MessageEvent<string>) => {
    // A malformed frame is ignored rather than thrown. This is a background
    // channel: an exception here surfaces as an unhandled rejection in someone's
    // console and changes nothing about the page, and the next real event still
    // works.
    let kind: unknown;
    try {
      kind = (JSON.parse(event.data) as { kind?: unknown }).kind;
    } catch {
      return;
    }
    if (kind === "mail" || kind === "extraction" || kind === "mailbox") announce(kind);
  });

  stream.addEventListener("error", () => {
    // `EventSource` does NOT always retry. It reconnects on its own after a
    // dropped connection or a clean end of stream — but an HTTP *error*
    // response, which is exactly what a restarting service returns, "fails the
    // connection" per the spec: `readyState` goes to CLOSED and the browser
    // never tries again. A single 502 during a deploy would therefore leave a
    // dashboard that looks fine and is silently dead for as long as the tab
    // stays open. That is the failure this handler exists for, and it was the
    // one hole left by trusting the built-in retry.
    //
    // CONNECTING means the browser IS retrying by itself; leave it alone rather
    // than racing it with a second connection.
    // CONNECTING here means the browser is retrying by itself, so the page is
    // not live — but nothing is known to be wrong yet either.
    if (stream.readyState !== EventSource.CLOSED) {
      setStatus("waiting");
      setOfflineSoon();
      return;
    }
    // Superseded, or closed on purpose by `close`. Not ours to reopen.
    if (source !== stream) return;

    setStatus("waiting");
    setOfflineSoon();

    source = null;
    stream.close();
    if (listeners.size === 0 || retrying !== null) return;

    retrying = setTimeout(() => {
      retrying = null;
      if (listeners.size > 0) open();
    }, retryIn);
    // Backed off, because the usual reason for a failed connection is a service
    // that is down — and a browser reconnecting every second is what turns an
    // outage into a worse outage.
    retryIn = Math.min(retryIn * 2, RETRY_MAX_MS);
  });
}

function close(): void {
  if (retrying !== null) {
    clearTimeout(retrying);
    retrying = null;
  }
  if (coalescing !== null) {
    clearTimeout(coalescing);
    coalescing = null;
    pending = new Set();
  }
  if (offlineTimer !== null) {
    clearTimeout(offlineTimer);
    offlineTimer = null;
  }
  retryIn = RETRY_BASE_MS;
  source?.close();
  source = null;
  // Back to the state a fresh page is in. Nobody is listening, so there is no
  // outage to report — and leaving "offline" behind would greet the next
  // subscriber with a warning about a stream that was closed on purpose.
  tell("waiting");
}

/**
 * The status light's source of truth.
 *
 * Subscribing does NOT open the stream — the light reports on a connection that
 * the data hooks are keeping, and a page that only wanted to show a dot has
 * nothing to be live about.
 */
export function useLiveStatus(): LiveStatus {
  const [current, setCurrent] = useState<LiveStatus>(status);

  useEffect(() => {
    // Read once on mount as well as subscribing: the stream is opened by the
    // data hooks, whose effects may have run and connected before this one, and
    // a watcher only hears about changes that happen after it arrives.
    setCurrent(status);
    const watcher = (next: LiveStatus) => setCurrent(next);
    watchers.add(watcher);
    return () => {
      watchers.delete(watcher);
    };
  }, []);

  return current;
}

/**
 * Refetch when the server says to, and when the tab comes back.
 *
 * `onNudge` is called with the kind so a caller can ignore the ones that do not
 * concern it — the activity panel has no reason to re-read itself because an
 * extraction finished.
 *
 * The visibility refresh is kept from the polling version and is not
 * redundant: a browser is free to suspend a background tab's connection
 * entirely, and a tab restored after an hour asleep may have an `EventSource`
 * that believes it is still open with an hour of silence behind it. Coming back
 * into view is the cheapest reliable moment to reconcile, and it costs one
 * request at the exact instant somebody started looking.
 */
export function useLive(onNudge: (nudge: Nudge) => void, enabled = true): void {
  // The callback lives in a ref so a caller can pass an inline closure without
  // the subscription being torn down and rebuilt on every render — which would
  // close and reopen the shared stream, repeatedly, and fire a catch-up refetch
  // each time it reopened.
  const handler = useRef(onNudge);
  useEffect(() => {
    handler.current = onNudge;
  }, [onNudge]);

  useEffect(() => {
    // A hook cannot be called conditionally, so the opting-out happens here
    // rather than at the call site. It matters: this hook is reached from
    // `useAuth`, which the nav on every page uses, and subscribing there would
    // hold a connection open for each visitor reading the pricing page.
    if (!enabled) return;

    const listener: Listener = (nudge) => handler.current(nudge);
    listeners.add(listener);
    open();

    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      handler.current("open");
      // Somebody is looking right now, so a stream still waiting out its backoff
      // should not keep them waiting with it. Reopening here is what makes the
      // worst case "stale until you look at it" rather than "stale for as long
      // as the backoff says".
      if (!source && listeners.size > 0) {
        if (retrying !== null) {
          clearTimeout(retrying);
          retrying = null;
        }
        open();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      listeners.delete(listener);
      document.removeEventListener("visibilitychange", onVisible);
      if (listeners.size === 0) close();
    };
  }, [enabled]);
}
