"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { OPPORTUNITIES_PATH, opportunityReviewPath } from "../api";
import { useLive } from "../events";

/**
 * One page of job orders, and the one place that talks to the opportunities
 * endpoint.
 *
 * The endpoint pages server-side now, so the page number is part of the
 * request rather than something the browser slices out of a complete list.
 * That has a consequence the UI has to be honest about: the search box and the
 * column sort below still work on what has been fetched, which is this page
 * and not the whole set. The table says so beside the count rather than
 * letting a filtered page of fifty pass for a filtered database.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

export type ReviewStatus = "ready" | "needs_review" | "reviewed";
export type QualityState = "verified" | "likely" | "needs_review";

/**
 * One shorthand code found in the email, and what the agency's glossary says
 * it means.
 *
 * `code` is verbatim from the message; `meaning` is the agency's own
 * definition and was never in the email. The character offsets are where it
 * appeared, so a disputed decoding can be traced back to the exact position in
 * the text rather than argued about.
 */
export type DecodedCode = {
  code: string;
  meaning: string;
  /** The protected characteristic the code refers to, or null for none. */
  attribute: string | null;
  start_char: number;
  end_char: number;
};

export type Opportunity = {
  id: string;
  received_datetime: string | null;
  company_name_raw: string | null;
  job_title_raw: string | null;
  salary_raw: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  working_hours_raw: string | null;
  requirements: string | null;
  job_description: string | null;
  duration_raw: string | null;
  location_raw: string | null;
  quality_state: QualityState;
  review_status: ReviewStatus;
  /** The message this was read out of. Shown as provenance, never as a link. */
  internet_message_id: string | null;
  graph_message_id: string | null;
  /** How many of the fields we look for the email actually stated. */
  verified_fields: number;
  total_fields: number;
  /** Shorthand found in the email. Optional: the endpoint gained these after
   *  the table shipped, so every reader treats absent as "none found" rather
   *  than assuming the key is there. */
  codes?: DecodedCode[];
  references_protected_attribute?: boolean;
};

export type Counts = { all: number; new: number; needs_review: number; reviewed: number };

/** The chips. `null` is "All" — the parameter is simply absent. */
export type Filter = null | "new" | "needs_review" | "reviewed";

type Page = {
  items: Opportunity[];
  total: number;
  limit: number;
  offset: number;
  counts: Counts;
};

export type ListState =
  | { status: "loading" }
  | { status: "ready"; page: Page }
  // Kept apart from an empty page, and never collapsed into one. A failed
  // fetch rendered as "no job orders" tells a recruiter their mailbox found
  // nothing, which is a claim about their business rather than about our
  // server.
  | { status: "unreadable"; message: string };

/** Fifty rows is about as much as anyone scans before paging anyway, and it
 *  keeps the client-side search over a page that is genuinely a page. */
export const PAGE_SIZE = 50;

const ZERO_COUNTS: Counts = { all: 0, new: 0, needs_review: 0, reviewed: 0 };

function listUrl(filter: Filter, offset: number): string {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
  if (filter) params.set("status", filter);
  return `${OPPORTUNITIES_PATH}?${params.toString()}`;
}

function messageFor(status: number): string {
  // A 401 is our session expiring, not the extraction failing. Saying "we
  // could not load your job orders" for an expired cookie sends someone to
  // look at the wrong thing entirely.
  return status === 401
    ? "Your session has expired. Sign in again to see your job orders."
    : "We could not load your job orders just now.";
}

export type Opportunities = {
  state: ListState;
  filter: Filter;
  offset: number;
  /** The last counts we were told, kept across a reload so the chips do not
   *  blink back to nothing every time a filter changes. */
  counts: Counts;
  setFilter: (filter: Filter) => void;
  setOffset: (offset: number) => void;
  /** Marks one row reviewed or not, and reports whether it worked. */
  review: (id: string, reviewed: boolean) => Promise<string | null>;
};

export function useOpportunities(): Opportunities {
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [filter, setFilterRaw] = useState<Filter>(null);
  const [offset, setOffset] = useState(0);
  const [counts, setCounts] = useState<Counts>(ZERO_COUNTS);

  // What the current request is for, readable from a callback that must not
  // change identity when the filter does. Assigned during render so a poll
  // firing between renders can never ask for the page we have just left.
  const asked = useRef({ filter, offset });
  asked.current = { filter, offset };

  // Every load takes a ticket, and only the newest one is allowed to write.
  // Three things race here — a filter change, a page change, and the poll — and
  // without this the slowest response wins regardless of which page the user is
  // actually on. `review` bumps it too, so a poll issued before a write cannot
  // land after it and put the old badge back.
  const generation = useRef(0);

  // How many visible loads are unresolved. A background refresh must not
  // overtake one: it would take the newer ticket, void the response the empty
  // table is waiting for, and then — since a quiet failure deliberately keeps
  // what is on screen — leave "Loading your job orders." there for good.
  // Declining costs nothing, because the request it would make is the one
  // already in flight. Very reachable: an event or a tab refocus arrives on the
  // server's schedule, which is exactly as likely to be mid-load as not.
  //
  // A count rather than a flag, and released unconditionally. Ownership by
  // "am I still the newest ticket?" looked tidier and was wrong: `review`
  // invalidates tickets too, so a write landing while a filter change was in
  // flight left the load superseded with no newer load to hand the flag to. It
  // stuck, and every later refresh declined forever — the exact stall the guard
  // exists to prevent, reached through the guard itself.
  const loud = useRef(0);

  const load = useCallback(async (quiet: boolean): Promise<void> => {
    if (quiet && loud.current > 0) return;
    if (!quiet) loud.current += 1;

    const mine = ++generation.current;
    const superseded = () => mine !== generation.current;
    const settle = () => {
      if (!quiet) loud.current -= 1;
    };

    // A background refresh never shows the loading state. Blanking the table
    // every fifteen seconds to fetch rows that are usually identical is worse
    // than the manual reload this replaces.
    if (!quiet) setState({ status: "loading" });

    try {
      const { filter: forFilter, offset: forOffset } = asked.current;
      const res = await fetch(listUrl(forFilter, forOffset), {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (superseded()) return;
      if (!res.ok) {
        // A failed poll keeps the page it has. The rows on screen were true
        // when they were fetched and are still the best thing we know; throwing
        // them away for a message about a blip that self-heals in fifteen
        // seconds loses real information to report a transient one. A 401 is
        // the exception — the session is gone, and every later poll will fail
        // the same way, so it has to be said.
        if (quiet && res.status !== 401) return;
        setState({ status: "unreadable", message: messageFor(res.status) });
        return;
      }
      const page = (await res.json()) as Page;
      if (superseded()) return;
      setState({ status: "ready", page });
      setCounts(page.counts);
    } catch {
      // A dropped connection on a poll is silent for the same reason: nothing
      // has changed about what we know, only about what we could confirm.
      if (superseded() || quiet) return;
      setState({ status: "unreadable", message: "We could not reach the server." });
    } finally {
      settle();
    }
  }, []);

  useEffect(() => {
    void load(false);
    // Unmounting invalidates whatever is in flight, which is what the
    // AbortController used to do — the ticket is void, so nothing writes.
    return () => {
      generation.current += 1;
    };
  }, [filter, offset, load]);

  // The table keeps up because the server says when to look, not because a timer
  // fired. `extraction` is the only kind that can add a row here — `mail` means
  // an email arrived, which becomes a vacancy only later, if at all, and
  // refetching on it would be two round trips to show the same page twice.
  useLive((nudge) => {
    if (nudge === "extraction" || nudge === "open") void load(true);
  });

  // Changing the filter must reset the page. Left alone, someone on page four
  // of "All" who clicks "Needs review" gets offset 150 of five rows — an empty
  // page that reads exactly like "there are none".
  const setFilter = useCallback((next: Filter) => {
    setFilterRaw(next);
    setOffset(0);
  }, []);

  // Kept in a ref rather than a dependency so `review` does not change
  // identity on every fetch and re-render every row that takes it as a prop.
  const stateRef = useRef(state);
  stateRef.current = state;

  const review = useCallback(async (id: string, reviewed: boolean): Promise<string | null> => {
    try {
      const res = await fetch(opportunityReviewPath(id), {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ reviewed }),
      });
      if (!res.ok) {
        return res.status === 401
          ? "Your session has expired. Sign in again, then mark this reviewed."
          : "We could not save that just now. Nothing has changed.";
      }
      const body = (await res.json()) as { review_status: ReviewStatus };

      // The write is now the newest truth, so any load still in flight — in
      // practice a poll issued a moment before the click — is void. Without
      // this, a response fetched before the POST can arrive after it and paint
      // the row back to unreviewed, and the next poll fifteen seconds later
      // would appear to fix it by itself.
      generation.current += 1;

      // Patch the row in place rather than refetching the page. A refetch
      // under an active "Needs review" filter would pull the row out from
      // under the panel the user is reading — the reward for marking something
      // done should not be losing your place.
      setState((current) =>
        current.status === "ready"
          ? {
              status: "ready",
              page: {
                ...current.page,
                items: current.page.items.map((row) =>
                  row.id === id ? { ...row, review_status: body.review_status } : row,
                ),
              },
            }
          : current,
      );
      // The chips have to move with it, and they are counts of the whole set,
      // not of this page — so they are asked for again rather than adjusted by
      // hand here, where a guess would drift from the database row by row.
      void refreshCounts(setCounts);
      return null;
    } catch {
      return "We could not reach the server. Nothing has changed.";
    }
  }, []);

  return { state, filter, offset, counts, setFilter, setOffset, review };
}

/**
 * The chip counts, on their own.
 *
 * `limit=1` because only the `counts` block is wanted; the one row that comes
 * back with it is thrown away. A failure is silent on purpose — the chips
 * simply keep the numbers they had. The alternative, an error banner over a
 * successful review, would report a problem the user does not have.
 */
async function refreshCounts(setCounts: (counts: Counts) => void): Promise<void> {
  try {
    const res = await fetch(`${OPPORTUNITIES_PATH}?limit=1&offset=0`, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) return;
    const body = (await res.json()) as { counts?: Counts };
    if (body.counts) setCounts(body.counts);
  } catch {
    /* keep the counts we have */
  }
}

/** Recent sync activity. Its own hook because its own endpoint, and because a
 *  failure here must not take the job orders down with it. */
export type ActivityEvent = {
  kind: string;
  outcome: "succeeded" | "failed";
  detail: string | null;
  at: string;
};

export type ActivityState =
  | { status: "loading" }
  | { status: "ready"; events: ActivityEvent[] }
  | { status: "unreadable"; message: string };

/**
 * Follows the stream unconditionally, because this panel is the one that answers
 * "is it working right now?" — the question most obviously wrong to answer with
 * a snapshot taken whenever the page happened to open. It listens to every kind:
 * it is a log of what ingestion did, and every kind is something ingestion did.
 */
export function useActivity(path: string): ActivityState {
  const [state, setState] = useState<ActivityState>({ status: "loading" });
  const generation = useRef(0);
  // Same rule as the list above: a background refresh that overtook the first
  // load and then failed quietly would leave this panel reading "Loading recent
  // activity." with nothing left in flight to replace it. A count, released
  // unconditionally, for the reason given there.
  const loud = useRef(0);

  const load = useCallback(
    async (quiet: boolean): Promise<void> => {
      if (quiet && loud.current > 0) return;
      if (!quiet) loud.current += 1;

      const mine = ++generation.current;
      const superseded = () => mine !== generation.current;
      const settle = () => {
        if (!quiet) loud.current -= 1;
      };

      try {
        const res = await fetch(path, {
          credentials: "include",
          headers: { Accept: "application/json" },
        });
        if (superseded()) return;
        if (!res.ok) {
          // Same rule as the list: a poll that fails keeps the events it has
          // rather than replacing a working panel with an error. An expired
          // session is permanent and gets said.
          if (quiet && res.status !== 401) return;
          setState({
            status: "unreadable",
            message:
              res.status === 401
                ? "Your session has expired. Sign in again to see this."
                : "We could not load recent activity.",
          });
          return;
        }
        const body = (await res.json()) as { events: ActivityEvent[] };
        if (superseded()) return;
        setState({ status: "ready", events: body.events ?? [] });
      } catch {
        if (superseded() || quiet) return;
        setState({ status: "unreadable", message: "We could not reach the server." });
      } finally {
        settle();
      }
    },
    [path],
  );

  useEffect(() => {
    void load(false);
    return () => {
      generation.current += 1;
    };
  }, [load]);

  useLive(() => void load(true));

  return state;
}
