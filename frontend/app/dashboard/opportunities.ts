"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  OPPORTUNITIES_PAGE_SIZE,
  OPPORTUNITIES_PATH,
  opportunityPath,
  opportunityReviewPath,
} from "../api";
import { useLive } from "../events";
import { DEFAULT_SORT, type Sort } from "./job-orders-table";
import { ApiError, readError } from "./candidates";

/**
 * One page of job orders, and the one place that talks to the opportunities
 * endpoint.
 *
 * Search and sort are server-side, same as `candidates.ts`: `q`, `sort` and
 * `descending` are query parameters, not something the browser slices out of
 * a page it already has.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

export type ReviewStatus = "ready" | "needs_review" | "reviewed";
export type QualityState = "verified" | "likely" | "needs_review";

/** What kind of placement this vacancy is. `null` means a recruiter has not
 *  classified it yet — never inferred, never guessed from the email. */
export type PlacementType =
  | "local_hire"
  | "mdw_work_permit"
  | "other_work_permit"
  | "s_pass"
  | "employment_pass";

export const PLACEMENT_TYPES: { value: PlacementType; label: string }[] = [
  { value: "local_hire", label: "Local hire" },
  { value: "mdw_work_permit", label: "MDW Work Permit" },
  { value: "other_work_permit", label: "Other Work Permit" },
  { value: "s_pass", label: "S Pass" },
  { value: "employment_pass", label: "Employment Pass" },
];

/** A genuine occupational requirement on the job itself — distinct from a
 *  candidate's own recorded sex and from a client's coded preference, which
 *  is decoded, flagged, and never acted on. `null` together with `null`
 *  reason is the ordinary case; the backend refuses one without the other. */
export type SexRequirement = "female" | "male";

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
  /** Nullable: not every job order has been classified for placement
   *  eligibility yet. Set only by a recruiter, via the form below. */
  placement_type: PlacementType | null;
  sex_requirement: SexRequirement | null;
  sex_requirement_reason: string | null;
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

const ZERO_COUNTS: Counts = { all: 0, new: 0, needs_review: 0, reviewed: 0 };

function listUrl(filter: Filter, offset: number, q: string, sort: Sort): string {
  const params = new URLSearchParams({
    limit: String(OPPORTUNITIES_PAGE_SIZE),
    offset: String(offset),
    sort: sort.key,
    descending: String(sort.descending),
  });
  if (filter) params.set("status", filter);
  if (q.trim()) params.set("q", q.trim());
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
  /** The box's own value, updated on every keystroke so typing never feels
   *  laggy. What actually reaches the server is `debouncedQ`, below. */
  q: string;
  sort: Sort;
  /** The last counts we were told, kept across a reload so the chips do not
   *  blink back to nothing every time a filter changes. */
  counts: Counts;
  /** A visible load — filter, search, sort or page change — is in flight over
   *  rows still on screen. Same reasoning as `candidates.ts`'s `refreshing`:
   *  collapsing back to `loading` on every keystroke or header click would
   *  unmount the table and the open detail panel for no reason. */
  refreshing: boolean;
  setFilter: (filter: Filter) => void;
  setOffset: (offset: number) => void;
  setQ: (q: string) => void;
  setSort: (sort: Sort) => void;
  /** Marks one row reviewed or not, and reports whether it worked. */
  review: (id: string, reviewed: boolean) => Promise<string | null>;
};

/** How long to wait after the last keystroke before the search actually
 *  reaches the server. Short enough that it still feels live, long enough
 *  that a five-letter word is one request, not five. */
const SEARCH_DEBOUNCE_MS = 300;

export function useOpportunities(): Opportunities {
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [filter, setFilterRaw] = useState<Filter>(null);
  const [offset, setOffset] = useState(0);
  const [q, setQRaw] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [sort, setSortRaw] = useState<Sort>(DEFAULT_SORT);
  const [counts, setCounts] = useState<Counts>(ZERO_COUNTS);
  const [refreshing, setRefreshing] = useState(true);

  // The value that actually reaches the server, held back from every
  // keystroke by `SEARCH_DEBOUNCE_MS`. Cleared on unmount of the timer, not
  // of the component, so a fast typist only ever fires the last one.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQ(q), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [q]);

  // What the current request is for, readable from a callback that must not
  // change identity when the filter does. Assigned during render so a poll
  // firing between renders can never ask for the page we have just left.
  const asked = useRef({ filter, offset, q: debouncedQ, sort });
  asked.current = { filter, offset, q: debouncedQ, sort };

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
    if (!quiet) {
      loud.current += 1;
      setRefreshing(true);
    }

    const mine = ++generation.current;
    const superseded = () => mine !== generation.current;
    const settle = () => {
      if (!quiet) {
        loud.current -= 1;
        if (loud.current === 0) setRefreshing(false);
      }
    };

    // A background refresh never shows the loading state, and neither does a
    // visible one once there are rows on screen — the same reasoning as
    // `candidates.ts`: a filter, search or sort change is a reload over what
    // is already showing, not a blank page. Only the genuine first load, with
    // nothing on screen yet, is a `loading` state.
    if (!quiet) setState((prev) => (prev.status === "ready" ? prev : { status: "loading" }));

    try {
      const { filter: forFilter, offset: forOffset, q: forQ, sort: forSort } = asked.current;
      const res = await fetch(listUrl(forFilter, forOffset, forQ, forSort), {
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
  }, [filter, offset, debouncedQ, sort, load]);

  // The table keeps up because the server says when to look, not because a timer
  // fired. `extraction` is the only kind that can add a row here — `mail` means
  // an email arrived, which becomes a vacancy only later, if at all, and
  // refetching on it would be two round trips to show the same page twice.
  useLive((nudge) => {
    if (nudge === "extraction" || nudge === "open") void load(true);
  });

  // Changing the filter, search or sort must reset the page. Left alone,
  // someone on page four of "All" who clicks "Needs review" — or types a
  // search that only matches five rows — gets offset 150 of five results, an
  // empty page that reads exactly like "there are none".
  const setFilter = useCallback((next: Filter) => {
    setFilterRaw(next);
    setOffset(0);
  }, []);
  const setQ = useCallback((next: string) => {
    setQRaw(next);
    setOffset(0);
  }, []);
  const setSort = useCallback((next: Sort) => {
    setSortRaw(next);
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

  return { state, filter, offset, q, sort, counts, refreshing, setFilter, setOffset, setQ, setSort, review };
}

/** What the placement form sends. `sex_requirement_reason` travels with
 *  `sex_requirement` as a pair — the backend's check constraint refuses one
 *  without the other, and sending them together means the browser never
 *  produces the half-set state the database would reject anyway. */
export type PlacementUpdate = {
  placement_type: PlacementType | null;
  sex_requirement: SexRequirement | null;
  sex_requirement_reason: string | null;
};

/** Sets a job order's placement type and/or sex requirement. Same PATCH
 *  pattern as `updateCandidate` in `candidates.ts`: partial body, JSON in and
 *  out, `ApiError` carrying whatever sentence the server worded — including
 *  the "a reason is required" refusal the backend enforces on this pair. */
export async function updateOpportunityPlacement(
  id: string,
  body: PlacementUpdate,
): Promise<Opportunity> {
  const res = await fetch(opportunityPath(id), {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Opportunity;
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
