"use client";

import {
  candidateSubmissionsPath,
  opportunitySourcingPath,
} from "../api";
import { ApiError, getCandidate, readError } from "./candidates";

/**
 * The one place that talks to the sourcing endpoints.
 *
 * Same split as `candidates.ts`: the types and the fetches live here, the
 * screen lives in `job-orders-sourcing.tsx`. Everything below goes out with
 * `credentials: "include"` and comes back through `readError`, so a refusal
 * the server took the trouble to word — the daily quota, a duplicate
 * submission — reaches the recruiter in the server's words rather than ours.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

/** The states a run moves through. `pending` and `running` are the only two
 *  worth asking again about; see `inFlight`. */
export type RunState = "pending" | "running" | "done" | "failed";

export type SourcingRun = {
  id: string;
  opportunity_id: string;
  state: RunState;
  /** Which client the already-submitted exclusion was applied against, or
   *  null when it could not be told — in which case the exclusion did not
   *  run at all and `client_unresolved_reason` says why. */
  client_id: string | null;
  client_unresolved_reason: string | null;
  candidates_considered: number | null;
  shortlisted: number | null;
  protected_attribute_noticed: boolean;
  protected_attribute_note: string | null;
  /** Whether this run narrowed its candidate pool to a single sex because the
   *  client's email shorthand (C/F, O/F, …) implied one, and — when it did —
   *  which sex. An action the run took, distinct from
   *  `protected_attribute_noticed`, which is what the explanation model saw. */
  sex_prefilter_applied: boolean;
  sex_prefilter_value: "female" | "male" | null;
  /** Null on a run the worker failed: only the route's enqueue-failure path
   *  writes a sentence. Never rendered as an empty error box. */
  failure_reason: string | null;
  created_at: string | null;
};

/**
 * One named signal's contribution to a score.
 *
 * Every number is a **string**, and deliberately: the column is NUMERIC and a
 * float round-trip would show `0.6499999999999999` for a value the scorer
 * computed exactly. Nothing here is ever parsed or compared as a number.
 *
 * `raw` and `contribution` are null together when there was nothing to
 * compare, which is not the same as scoring zero — `note` then says what was
 * missing, and the row must say so rather than drawing an empty bar.
 */
export type MatchReason = {
  name: string;
  weight: string;
  raw: string | null;
  contribution: string | null;
  note: string | null;
};

/**
 * One person on the shortlist.
 *
 * `candidate_id` and no name: these routes never join the candidate record,
 * so the screen fetches the names itself (`namesFor` below). `score` is a
 * string for the reason `MatchReason`'s numbers are, and the order the server
 * sent — score descending, then candidate id — is the order to render. Never
 * re-sorted here: sorting strings numerically is a bug waiting to happen, and
 * the server has already done it properly.
 */
export type SourcingMatch = {
  candidate_id: string;
  score: string;
  /**
   * `false` for a candidate the caller does not own or hold a share on.
   * Sourcing scores the whole agency's book on purpose — an agency has to be
   * able to shortlist across its own database — but the API redacts a match
   * the caller cannot see down to the same tier the create-collision 409
   * uses: `full_name` arrives already abbreviated and contact-masked, `reasons`,
   * `explanation` and `explanation_evidence` are absent rather than null, and
   * `held_by` plus `can_request_access` name the colleague to ask instead.
   * Absent on an older server response, so treated as visible by default —
   * the redaction is new behavior, not a field every response is guaranteed
   * to carry.
   */
  visible?: boolean;
  /** Only present on a redacted match — the abbreviated, contact-masked name
   *  the server computed. Render verbatim; never expand or prettify it. */
  full_name?: string | null;
  /** Only present on a redacted match — who owns it. */
  held_by?: string | null;
  /** Only present on a redacted match. */
  can_request_access?: boolean;
  reasons: MatchReason[] | null;
  explanation: string | null;
  explanation_evidence: string | null;
};

export type SourcingView = { run: SourcingRun | null; matches: SourcingMatch[] };

/** Whether a run is still worth asking about. Anything else is a final state
 *  and polling it is a request whose answer cannot change. */
export function inFlight(run: SourcingRun | null): boolean {
  return run !== null && (run.state === "pending" || run.state === "running");
}

export async function startSourcing(opportunityId: string): Promise<SourcingRun> {
  const res = await fetch(opportunitySourcingPath(opportunityId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  // 429 is the daily quota, and the server words it with the actual number in
  // it. `readError` returns that sentence, so the quota reads as a limit
  // someone can plan around rather than as a generic failure.
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as SourcingRun;
}

export async function getSourcing(opportunityId: string): Promise<SourcingView> {
  const res = await fetch(opportunitySourcingPath(opportunityId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as SourcingView;
}

export type Submission = {
  id: string;
  candidate_id: string;
  client_id: string;
  opportunity_id: string | null;
  submitted_at: string | null;
};

export async function recordSubmission(
  candidateId: string,
  clientId: string,
  opportunityId: string,
): Promise<Submission> {
  const res = await fetch(candidateSubmissionsPath(candidateId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: clientId, opportunity_id: opportunityId }),
  });
  // Two different refusals now arrive as a 409, and they differ only in
  // prose — there is no code to switch on:
  //
  //   * a repeat, meaning the row is already there, which is what the
  //     recruiter wanted but did not know;
  //   * the client is suspended, and the message carries the reason someone
  //     typed when they put it on hold.
  //
  // So the server's `detail` is what has to reach the screen. A caller that
  // hardcodes "already submitted" for a 409 would tell a recruiter the
  // opposite of what happened on the second one.
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Submission;
}

/**
 * The names behind a shortlist's candidate ids.
 *
 * The matches carry ids only, and a list of UUIDs is not a shortlist. One
 * request per person rather than a list call: the stored shortlist is capped
 * server-side at the top few, the candidate list endpoint cannot be asked for
 * a specific set of ids, and paging through everyone to find ten is worse than
 * ten small reads. Already-known ids are skipped, so a poll that returns the
 * same shortlist again costs nothing.
 *
 * A name that cannot be read is left out rather than faked — the caller shows
 * the id, which at least remains traceable. One archived or since-deleted
 * candidate must not take the whole shortlist down with it.
 */
export async function namesFor(
  ids: string[],
  known: ReadonlyMap<string, string>,
): Promise<Map<string, string>> {
  const wanted = ids.filter((id) => !known.has(id));
  const found = new Map(known);
  await Promise.all(
    wanted.map(async (id) => {
      try {
        const candidate = await getCandidate(id);
        found.set(id, candidate.full_name);
      } catch {
        /* left out on purpose; the row falls back to the id */
      }
    }),
  );
  return found;
}
