"use client";

import { opportunityIntelligencePath } from "../api";
import { ApiError, readError } from "./candidates";

/**
 * The one place that talks to the Job Intelligence endpoints.
 *
 * The analysis runs as a background job: POST creates a `pending` row and
 * returns 202 immediately (the three Cerebras calls run in the worker, not the
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

/** All three stages, present only when `state === "done"`. */
export type Intelligence = {
  understanding: Understanding;
  persona: Persona;
  search_plan: SearchPlan;
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
};

/** What GET returns when no analysis exists for this job order yet. */
export type NoIntelligence = { intelligence: null };

/**
 * Start (or re-run) the analysis. Returns 202 with the `pending` row — the
 * answer arrives on a later GET, not in this response.
 */
export async function runIntelligence(
  opportunityId: string,
): Promise<IntelligenceView> {
  const res = await fetch(opportunityIntelligencePath(opportunityId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
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

/** Whether a view is still working and worth polling for. */
export function inFlight(view: IntelligenceView | NoIntelligence | null): boolean {
  if (!view || view.intelligence === null) return false;
  return view.state === "pending" || view.state === "running";
}
