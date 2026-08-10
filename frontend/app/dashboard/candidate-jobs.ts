"use client";

import { useCallback, useEffect, useState } from "react";

import { candidateJobsPath } from "../api";
import { ApiError, readError } from "./candidates";

/**
 * The "Find Job" shortlist: the best-fitting job orders for one candidate.
 *
 * The two halves of a saved result, mirroring `candidate-intelligence.ts`:
 * POST runs the matcher and saves the snapshot (one row per candidate, so a
 * re-run replaces rather than appends), and GET reads the saved snapshot back
 * so the Jobs tab reopens to the last result without re-scoring. GET answers
 * the same shape with `saved_at: null` when Find Job has never run, so "not
 * run yet" is distinguishable from "ran, no matches".
 *
 * Same split as `sourcing.ts`: the types, the fetch and the hook live here,
 * the screen lives in the candidate modal (`candidates/candidate-panel.tsx`).
 * The fetches go out with `credentials: "include"` and come back through
 * `readError`, so a refusal the server worded reaches the recruiter as
 * written.
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
  /** The candidate's salary expectation, when fully recorded (amount,
   *  currency and period all present). Null means the salary component
   *  abstains on the candidate's side for every job order — a
   *  candidate-level fact the screen should state once, not per card. */
  candidate_salary: {
    amount: number;
    currency: string;
    period: string;
  } | null;
  /** When the snapshot was saved — null when Find Job has never run for this
   *  candidate, so "not run yet" reads differently from "ran, no matches". */
  saved_at: string | null;
};

/** Run Find Job for one candidate and save the result. The button's action. */
export async function runCandidateJobs(candidateId: string): Promise<CandidateJobs> {
  const res = await fetch(candidateJobsPath(candidateId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateJobs;
}

/** Read the last saved shortlist back. The tab's action. */
export async function getCandidateJobs(candidateId: string): Promise<CandidateJobs> {
  const res = await fetch(candidateJobsPath(candidateId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateJobs;
}

/**
 * The Find Job state for one candidate, owned in one place.
 *
 * Extracted from the panel so the "Find Job" button can live in the modal
 * header while the Jobs tab reads the same saved result — both need the same
 * `run()`, the same `starting` flag, and the same mount read of whatever was
 * last saved. Unlike `useCandidateIntelligence` there is no polling: the
 * matcher is synchronous, so `run()` finishes with the answer in hand.
 */
export type CandidateJobsPhase =
  | { status: "loading" }
  | { status: "idle"; view: CandidateJobs }
  | { status: "error"; message: string };

export function useCandidateJobs(candidateId: string): {
  phase: CandidateJobsPhase;
  run: () => Promise<void>;
  starting: boolean;
  runError: string | null;
} {
  const [phase, setPhase] = useState<CandidateJobsPhase>({ status: "loading" });
  const [starting, setStarting] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  // Stable across renders unless the candidate changes, so the mount effect
  // fires once per candidate.
  const refetch = useCallback(async () => {
    try {
      const view = await getCandidateJobs(candidateId);
      setPhase({ status: "idle", view });
    } catch (err) {
      setPhase({
        status: "error",
        message:
          err instanceof Error ? err.message : "We could not read this shortlist just now.",
      });
    }
  }, [candidateId]);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  const run = useCallback(async () => {
    setStarting(true);
    setRunError(null);
    try {
      const view = await runCandidateJobs(candidateId);
      setPhase({ status: "idle", view });
    } catch (err) {
      setRunError(
        err instanceof Error ? err.message : "We could not run Find Job just now.",
      );
    } finally {
      setStarting(false);
    }
  }, [candidateId]);

  return { phase, run, starting, runError };
}
