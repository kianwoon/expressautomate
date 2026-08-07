"use client";

import { useCallback, useEffect, useState } from "react";

import { candidateIntelligencePath, SOURCING_POLL_MS } from "../api";
import { ApiError, readError } from "./candidates";

/**
 * The one place that talks to the Candidate Intelligence endpoints.
 *
 * The analysis runs as a background job: POST creates a `pending` row and
 * returns 202 immediately (the five Cerebras calls run in the worker, not the
 * request), and GET reads the row back in whatever state it is in. The panel
 * polls GET until the row is terminal, exactly as the Job Intelligence panel
 * polls and the sourcing panel polls a run.
 *
 * Mirrors `job-intelligence.ts` in shape — one intelligence idiom in this
 * codebase, not two.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

/** The states an analysis moves through. `pending` and `running` are the only
 *  two worth asking again about; see `inFlight`. */
export type CandidateIntelligenceState = "pending" | "running" | "done" | "failed";

/** One decomposed piece of work a role involved (design doc L2). Mirrors
 *  `WorkItem` on the server. */
export type WorkItem = {
  task: string;
  tool: string;
  judgment_level: string;
  accountability: string;
};

/** One role from the candidate's history, with its work decomposed (L1 + L2).
 *  Mirrors `HistoryRole`. */
export type HistoryRole = {
  period: string;
  title: string;
  domain: string;
  seniority: string;
  scope: string;
  work: WorkItem[];
  evidence: string;
};

/** Pass 1 — the candidate's history, value-neutral (L1 + L2). Mirrors
 *  `HistoryProfile`. */
export type HistoryProfile = {
  roles: HistoryRole[];
  industries: string[];
  functions: string[];
  systems: string[];
  trajectory: string[];
};

/** One capability assessed for automation exposure + residual human value
 *  (L3 + L4). Mirrors `AutomationAssessment`. */
export type AutomationAssessment = {
  capability: string;
  automation_level: string;
  automation_reason: string;
  residual_human_value: string;
};

/** Pass 2 — automation exposure across capabilities (L3 + L4). Mirrors
 *  `AutomationProfile`. */
export type AutomationProfile = {
  assessments: AutomationAssessment[];
  scarce_capabilities: string[];
};

/** Pass 3 — today's version of the work family (L5 + L6). Mirrors
 *  `MarketBenchmark`. */
export type MarketBenchmark = {
  work_family: string;
  current_work: string[];
  current_required: string[];
  declining: string[];
  emerging: string[];
  scarce: string[];
  automation_summary: string;
};

/** One capability assessed against today's standard (L6 gap). Mirrors
 *  `CapabilityGap`. */
export type CapabilityGap = {
  capability: string;
  status: string;
  note: string;
};

/** Pass 4 — gaps between the candidate and today's standard (L6). Mirrors
 *  `GapAnalysis`. */
export type GapAnalysis = {
  gaps: CapabilityGap[];
  evidence_gaps: string[];
};

/** Pass 5 — the decomposable residual value + candid profile (L7 + L8).
 *  Mirrors `ResidualValueAssessment`. */
export type ResidualValueAssessment = {
  historical_strength: string;
  automation_exposure: string;
  current_relevance: string;
  scarce_capabilities: string[];
  depreciated_capabilities: string[];
  emerging_capabilities: string[];
  evidence_gaps: string[];
  overall_assessment: string;
  current_profile: string;
};

/** All five stages, present only when `state === "done"`. */
export type CandidateIntelligence = {
  history: HistoryProfile;
  automation: AutomationProfile;
  benchmark: MarketBenchmark;
  gaps: GapAnalysis;
  residual: ResidualValueAssessment;
};

/** The full row, as GET and POST return it. */
export type CandidateIntelligenceView = {
  id: string;
  state: CandidateIntelligenceState;
  failure_reason: string | null;
  analysed_at: string | null;
  /** `null` until the row is `done`. */
  intelligence: CandidateIntelligence | null;
};

/** What GET returns when no analysis exists for this candidate yet. */
export type NoCandidateIntelligence = { intelligence: null };

/**
 * Start (or re-run) the analysis. Returns 202 with the `pending` row — the
 * answer arrives on a later GET, not in this response.
 */
export async function runCandidateIntelligence(
  candidateId: string,
): Promise<CandidateIntelligenceView> {
  const res = await fetch(candidateIntelligencePath(candidateId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateIntelligenceView;
}

/** Read the row back, or `{ intelligence: null }` if none exists yet. */
export async function getCandidateIntelligence(
  candidateId: string,
): Promise<CandidateIntelligenceView | NoCandidateIntelligence> {
  const res = await fetch(candidateIntelligencePath(candidateId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateIntelligenceView | NoCandidateIntelligence;
}

/** Whether a view is still working and worth polling for.

  See `inFlight` in `job-intelligence.ts` for the full reasoning: a `pending`
  or `running` row has `intelligence: null`, so checking `intelligence` would
  wrongly stop the poller the moment a run starts. The `state` field decides. */
export function candidateIntelInFlight(
  view: CandidateIntelligenceView | NoCandidateIntelligence | null,
): boolean {
  if (!view || !("state" in view)) return false;
  return view.state === "pending" || view.state === "running";
}

/**
 * The analysis state for one candidate, owned in one place.
 *
 * Extracted from the panel so the "Run analysis" button can live in the modal
 * header while the three stage tabs read the same result — both need the same
 * `run()`, the same `starting`/`waiting` flags, and the same polling loop.
 * Mirrors `useJobIntelligence` in `job-intelligence.ts`.
 */
export type CandidateIntelligencePhase =
  | { status: "loading" }
  | { status: "idle"; view: CandidateIntelligenceView | NoCandidateIntelligence }
  | { status: "error"; message: string };

export function useCandidateIntelligence(candidateId: string): {
  phase: CandidateIntelligencePhase;
  run: () => Promise<void>;
  starting: boolean;
  waiting: boolean;
  runError: string | null;
  view: CandidateIntelligenceView | NoCandidateIntelligence | null;
  analysis: CandidateIntelligence | null;
} {
  const [phase, setPhase] = useState<CandidateIntelligencePhase>({
    status: "loading",
  });
  const [starting, setStarting] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  // Stable across renders unless the candidate changes, so the mount effect
  // fires once per candidate and the poll effect's interval is not torn down
  // each render.
  const refetch = useCallback(async () => {
    try {
      const v = await getCandidateIntelligence(candidateId);
      setPhase({ status: "idle", view: v });
    } catch (err) {
      setPhase({
        status: "error",
        message: err instanceof Error ? err.message : "We could not read this analysis just now.",
      });
    }
  }, [candidateId]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  const view = phase.status === "idle" ? phase.view : null;
  const waiting = candidateIntelInFlight(view);

  // Poll only while the row is `pending` or `running`. A `done` or `failed`
  // row is a finished record — asking again is a request whose answer cannot
  // change, every open panel, forever, for nothing.
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => void refetch(), SOURCING_POLL_MS);
    return () => clearInterval(timer);
  }, [waiting, refetch]);

  const run = useCallback(async () => {
    setStarting(true);
    setRunError(null);
    try {
      const started = await runCandidateIntelligence(candidateId);
      setPhase({ status: "idle", view: started });
    } catch (err) {
      setRunError(
        err instanceof Error ? err.message : "The analysis could not start just now.",
      );
    } finally {
      setStarting(false);
    }
  }, [candidateId]);

  const analysis =
    view && "intelligence" in view && view.intelligence ? view.intelligence : null;

  return { phase, run, starting, waiting, runError, view, analysis };
}
