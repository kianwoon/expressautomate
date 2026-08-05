"use client";

import { opportunityIntelligencePath } from "../api";
import { ApiError, readError } from "./candidates";

/**
 * The one place that talks to the Job Intelligence endpoints.
 *
 * Same split as `sourcing.ts`: the types and the fetches live here, the
 * component lives in `job-intelligence-panel.tsx`. Everything goes out with
 * `credentials: "include"` and comes back through `readError`, so a refusal the
 * server worded — "not configured", "no title to analyse" — reaches the
 * recruiter in the server's words rather than ours.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

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

/** All three stages, as both POST and GET return them. */
export type Intelligence = {
  understanding: Understanding;
  persona: Persona;
  search_plan: SearchPlan;
};

/** What GET returns: the analysis, or `null` when none has been run yet. */
export type IntelligenceView =
  | { intelligence: Intelligence; removed_codes: string[] | null; analysed_at: string | null }
  | { intelligence: null };

/**
 * Run the analysis. Synchronous on the server (the three calls are fast), so
 * the response carries the result rather than a run id to poll. Re-running
 * replaces the previous analysis rather than accumulating rows.
 */
export async function runIntelligence(opportunityId: string): Promise<IntelligenceView> {
  const res = await fetch(opportunityIntelligencePath(opportunityId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as IntelligenceView;
}

/** Read the stored analysis back, or `{ intelligence: null }` if none yet. */
export async function getIntelligence(opportunityId: string): Promise<IntelligenceView> {
  const res = await fetch(opportunityIntelligencePath(opportunityId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as IntelligenceView;
}
