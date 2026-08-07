"use client";

import { useCallback, useEffect, useState } from "react";

import { candidateIntelligencePath, SOURCING_POLL_MS } from "../api";
import { ApiError, readError } from "./candidates";

/**
 * The one place that talks to the Candidate Intelligence endpoints.
 *
 * The analysis runs as a background job: POST creates a `pending` row and
 * returns 202 immediately (the three Cerebras calls run in the worker, not the
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

/** Stage 1 — the candidate's career as a structured progression. Mirrors
 *  `CareerProfile` on the server. */
export type TimelineEntry = {
  period: string;
  title: string;
  domain: string;
};

export type CareerProfile = {
  timeline: TimelineEntry[];
  trajectory: string[];
  primary_domain: string;
  secondary_domains: string[];
  career_direction: string;
  career_stage: string;
};

/** Stage 2 — evidence-backed capabilities. Mirrors `CapabilityProfile`. */
export type CapabilityEntry = {
  capability: string;
  category: string;
  confidence: number;
  supporting_evidence: string;
};

export type CapabilityProfile = {
  capabilities: CapabilityEntry[];
  tools: string[];
};

/** Stage 3 — the synthesised professional profile. Mirrors
 *  `ProfessionalProfile`. */
export type RoleAffinity = {
  role: string;
  affinity_type: string;
  confidence: number;
};

export type ProfessionalProfile = {
  professional_identity: string;
  specializations: string[];
  orientation: string;
  role_affinity: RoleAffinity[];
};

/** All three stages, present only when `state === "done"`. */
export type CandidateIntelligence = {
  career: CareerProfile;
  capability: CapabilityProfile;
  profile: ProfessionalProfile;
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
