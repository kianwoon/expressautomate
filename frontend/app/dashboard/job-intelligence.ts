"use client";

import { useCallback, useEffect, useState } from "react";

import { opportunityIntelligencePath, SOURCING_POLL_MS } from "../api";
import { ApiError, readError } from "./candidates";

/**
 * The one place that talks to the Job Intelligence endpoints.
 *
 * The analysis runs as a background job: POST creates a `pending` row and
 * returns 202 immediately (the three DeepSeek calls run in the worker, not the
 * request), and GET reads the row back in whatever state it is in. The panel
 * polls GET until the row is terminal, exactly as the sourcing panel polls a
 * run.
 *
 * Everything goes out with `credentials: "include"` and comes back through
 * `readError`, so a refusal the server worded reaches the recruiter in the
 * server's words rather than ours.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

/** The states an analysis moves through. `pending` and `running` are the only
 *  two worth asking again about; see `inFlight`. */
export type IntelligenceState = "pending" | "running" | "done" | "failed";

/** Module 1 — what the work is. Mirrors `JDUnderstanding` on the server. */
export type Understanding = {
  role: string;
  business_purpose: string;
  daily_activities: string[];
  work_environment: string;
  must_have_requirements: string[];
  preferred_requirements: string[];
  working_conditions: string;
  success_characteristics: string[];
  potential_challenges: string[];
  confidence: number;
};

/** Module 2 — who would do this work well. Mirrors `CandidatePersona`. */
export type Persona = {
  likely_backgrounds: string[];
  transferable_roles: string[];
  transferable_industries: string[];
  behaviours: string[];
  communication_style: string;
  career_stage: string;
  motivations: string[];
  salary_expectation: string;
  availability: string;
};

/** Module 3 — how to look for that person. Mirrors `SearchPlan`. */
export type SearchPlan = {
  platform: string;
  priority: number;
  queries: string[];
  negative_queries: string[];
  salary: string;
  location: string;
  employment_type: string;
};

/** Module 4 — the matched MOM occupation and its wage percentiles.
 *  Mirrors `OccupationMatch` on the server. Present only when the occupation
 *  stage found a match; absent when it degraded (empty library, no embeddings). */
export type OccupationMatch = {
  title: string;
  year: number;
  gross_p25: number;
  gross_p50: number;
  gross_p75: number;
  basic_p25: number;
  basic_p50: number;
  basic_p75: number;
  similarity: number;
  confidence: number;
  rationale: string;
};

/** All four stages, present only when `state === "done"`. */
export type Intelligence = {
  understanding: Understanding;
  persona: Persona;
  search_plan: SearchPlan;
  /** `null` when the occupation stage degraded to no match. */
  occupation?: OccupationMatch | null;
};

/** The full row, as GET and POST return it. */
export type IntelligenceView = {
  id: string;
  state: IntelligenceState;
  failure_reason: string | null;
  analysed_at: string | null;
  /** `null` until the row is `done`. */
  intelligence: Intelligence | null;
  /** Present only on a `done` row. */
  removed_codes?: string[] | null;
  /** Present only on a `failed` row: true when the failure is the thin-context
   *  guard (the order has no description, requirements or skills) — the one
   *  failure "Run anyway" can override. A machine flag, never prose-matched. */
  thin?: boolean | null;
};

/** What GET returns when no analysis exists for this job order yet. */
export type NoIntelligence = { intelligence: null };

/**
 * Start (or re-run) the analysis. Returns 202 with the `pending` row — the
 * answer arrives on a later GET, not in this response.
 *
 * `allowThin` is the "Run anyway" override: the worker then skips its
 * thin-order pre-flight and answers the order from the title alone, at low
 * confidence. The server stores it on the row, so a poll-inherited re-enqueue
 * keeps the recruiter's choice.
 */
export async function runIntelligence(
  opportunityId: string,
  allowThin = false,
): Promise<IntelligenceView> {
  const res = await fetch(
    opportunityIntelligencePath(opportunityId) +
      (allowThin ? "?allow_thin=true" : ""),
    {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json" },
    },
  );
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as IntelligenceView;
}

/** Read the row back, or `{ intelligence: null }` if none exists yet. */
export async function getIntelligence(
  opportunityId: string,
): Promise<IntelligenceView | NoIntelligence> {
  const res = await fetch(opportunityIntelligencePath(opportunityId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as IntelligenceView | NoIntelligence;
}

/** Whether a view is still working and worth polling for.

  A `pending` or `running` row has `intelligence: null` (the results are not
  written yet), so checking `intelligence` would wrongly stop the poller the
  moment a run starts — leaving the panel stuck on "starting" until the modal
  was reopened. The `state` field is what decides: `NoIntelligence` has none. */
export function inFlight(view: IntelligenceView | NoIntelligence | null): boolean {
  if (!view || !("state" in view)) return false;
  return view.state === "pending" || view.state === "running";
}

/**
 * The analysis state for one job order, owned in one place.
 *
 * Extracted from the panel so the "Run analysis" button can live in the modal
 * header while the three stage tabs read the same result — both need the same
 * `run()`, the same `starting`/`waiting` flags, and the same polling loop, and
 * duplicating that across the header and the tabs is how they drift. One hook,
 * called once from `Detail`, returns everything both need.
 *
 * `tab` is the caller's current tab string — returned only so the hook can
 * report it back, never set by the hook itself. Tab selection is the caller's
 * concern (it owns the `activeTab` state); the hook owns only the analysis.
 */
export type JobIntelligencePhase =
  | { status: "loading" }
  | { status: "idle"; view: IntelligenceView | NoIntelligence }
  | { status: "error"; message: string };

export function useJobIntelligence(rowId: string): {
  phase: JobIntelligencePhase;
  run: () => Promise<void>;
  runAnyway: () => Promise<void>;
  starting: boolean;
  waiting: boolean;
  runError: string | null;
  view: IntelligenceView | NoIntelligence | null;
  analysis: Intelligence | null;
} {
  const [phase, setPhase] = useState<JobIntelligencePhase>({ status: "loading" });
  const [starting, setStarting] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  // Stable across renders unless the row changes, so the mount effect fires
  // once per row and the poll effect's interval is not torn down each render.
  const refetch = useCallback(async () => {
    try {
      const v = await getIntelligence(rowId);
      setPhase({ status: "idle", view: v });
    } catch (err) {
      setPhase({
        status: "error",
        message: err instanceof Error ? err.message : "We could not read this analysis just now.",
      });
    }
  }, [rowId]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  const view = phase.status === "idle" ? phase.view : null;
  const waiting = inFlight(view);

  // Poll only while the row is `pending` or `running`. A `done` or `failed`
  // row is a finished record — asking again is a request whose answer cannot
  // change, every open panel, forever, for nothing.
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => void refetch(), SOURCING_POLL_MS);
    return () => clearInterval(timer);
  }, [waiting, refetch]);

  const run = useCallback(
    async (allowThin: boolean) => {
      setStarting(true);
      setRunError(null);
      try {
        const started = await runIntelligence(rowId, allowThin);
        setPhase({ status: "idle", view: started });
      } catch (err) {
        setRunError(
          err instanceof Error ? err.message : "The analysis could not start just now.",
        );
      } finally {
        setStarting(false);
      }
    },
    [rowId],
  );

  /** The plain run — the header button and the guard-refused re-run. */
  const runNormal = useCallback(() => run(false), [run]);
  /** The "Run anyway" escape hatch for a thin-order refusal. */
  const runAnyway = useCallback(() => run(true), [run]);

  const analysis =
    view && "intelligence" in view && view.intelligence ? view.intelligence : null;

  return { phase, run: runNormal, runAnyway, starting, waiting, runError, view, analysis };
}

