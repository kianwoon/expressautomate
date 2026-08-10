"use client";

import { candidateJobsPath } from "../api";
import { ApiError, readError } from "./candidates";

/**
 * The "Find Job" shortlist: the best-fitting job orders for one candidate.
 *
 * Same split as `sourcing.ts`: the types and the fetch live here, the screen
 * lives in the candidate modal (`candidates/candidate-panel.tsx`). The fetch
 * goes out with `credentials: "include"` and comes back through `readError`,
 * so a refusal the server worded reaches the recruiter as written.
 */

/** One named signal's contribution to a score.
 *
 * Every number is a **string**, deliberately — the wire contract the sourcing
 * run keeps, and for the same reason: the column is NUMERIC and a float
 * round-trip would show `0.6499999999999999` for a value the scorer computed
 * exactly. The screen parses them for display (as percentages), never to
 * compare: ordering is the server's job, and stays string-stable.
 *
 * `raw` and `contribution` are null together when there was nothing to
 * compare, which is not the same as scoring zero — `note` then says what was
 * missing, and the row must say so rather than drawing an empty bar.
 */
export type CandidateJobReason = {
  name: string;
  weight: string;
  raw: string | null;
  contribution: string | null;
  note: string | null;
};

/** One shortlisted job order, with enough of the record to render a card.
 *
 * `score` is a string for the reason `CandidateJobReason`'s numbers are, and
 * the order the server sent — score descending, then job-order id — is the
 * order to render. Never re-sorted here.
 */
export type CandidateJobMatch = {
  id: string;
  company_name_raw: string | null;
  job_title_raw: string | null;
  location_raw: string | null;
  salary_raw: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  working_hours_raw: string | null;
  duration_raw: string | null;
  requirements: string | null;
  employment_type: string | null;
  assigned_user_id: string | null;
  review_status: "new" | "needs_review" | "reviewed";
  quality_state: "needs_review" | "likely" | "verified";
  received_datetime: string | null;
  score: string;
  reasons: CandidateJobReason[] | null;
};

export type CandidateJobs = {
  /** The shortlist, best first. At most the server's configured cap. */
  items: CandidateJobMatch[];
  /** How many visible current revisions were examined — the denominator the
   *  shortlist was cut from. */
  considered: number;
  /** How many of those produced a score. The gap between `considered` and
   *  `scored` is the vacancies with nothing comparable on record. */
  scored: number;
  limit: number;
};

/** The best-fitting visible job orders for one candidate, best first. */
export async function findCandidateJobs(id: string): Promise<CandidateJobs> {
  const res = await fetch(candidateJobsPath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateJobs;
}
