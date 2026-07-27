"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { OPPORTUNITIES_PATH, opportunityReviewPath } from "../api";

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

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    (async () => {
      try {
        const res = await fetch(listUrl(filter, offset), {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) {
          setState({ status: "unreadable", message: messageFor(res.status) });
          return;
        }
        const page = (await res.json()) as Page;
        setState({ status: "ready", page });
        setCounts(page.counts);
      } catch {
        // An aborted fetch is this component unmounting or the filter moving
        // on, not a failure. Left in "loading": there is nobody to tell.
        if (!controller.signal.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
        }
      }
    })();
    return () => controller.abort();
  }, [filter, offset]);

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

export function useActivity(path: string): ActivityState {
  const [state, setState] = useState<ActivityState>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await fetch(path, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) {
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
        setState({ status: "ready", events: body.events ?? [] });
      } catch {
        if (!controller.signal.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
        }
      }
    })();
    return () => controller.abort();
  }, [path]);

  return state;
}
