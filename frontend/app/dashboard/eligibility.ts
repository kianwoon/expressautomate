"use client";

import { opportunityCandidateEligibilityPath } from "../api";
import { ApiError, readError } from "./candidates";

/**
 * Whether one candidate meets the placement requirements for one job order —
 * a compliance fact (MOM Work Permit eligibility, or a job's own stated
 * occupational requirement), never a hiring recommendation.
 *
 * Four outcomes, and they are not degrees of the same thing:
 * - `met` / `not_met` are verdicts against a rule that applies.
 * - `unknown` means the rule applies but the record cannot say — a gap in
 *   *our* data, not a fact about the person.
 * - `not_applicable` means the rule itself does not apply to this placement
 *   type (age/education/nationality mean nothing for a local hire). Nothing
 *   is missing and nothing is wrong.
 *
 * Collapsing `unknown` and `not_applicable` together would send a recruiter
 * hunting for a date of birth that nothing requires — the two are opposite
 * claims wearing the same shrug, so nothing in this module or its renderer
 * ever merges them.
 *
 * allow-hardcode: the strings below are user-facing copy, not a list
 * anything is matched against.
 */

export type EligibilityCriterion =
  | "sex"
  | "age"
  | "education"
  | "nationality"
  | "occupational_sex_requirement";

export type EligibilityOutcome = "met" | "not_met" | "unknown" | "not_applicable";

/** `regulatory` — MOM's Work Permit rule. `occupational` — this job's own
 *  stated requirement, with the recruiter's reason. The two must never be
 *  presented as if they were the same kind of claim. */
export type EligibilityBasis = "regulatory" | "occupational";

export type EligibilityFinding = {
  criterion: EligibilityCriterion;
  outcome: EligibilityOutcome;
  basis: EligibilityBasis;
  detail: string;
};

export type Eligibility = {
  placement_type: string | null;
  /** False, with empty `findings`, when the job order has no `placement_type`
   *  set yet. Never a pass — the caller must not render this as "eligible". */
  assessable: boolean;
  /** The date age was judged as of. MOM judges at application, so a
   *  candidate who is 49 today may be 50 by the time a permit is filed —
   *  "eligible" with no date attached is a claim about a moment already
   *  gone. Null when nothing was assessable (e.g. `assessable: false`). */
  evaluated_as_of: string | null;
  findings: EligibilityFinding[];
};

export async function getEligibility(
  opportunityId: string,
  candidateId: string,
): Promise<Eligibility> {
  const res = await fetch(opportunityCandidateEligibilityPath(opportunityId, candidateId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Eligibility;
}

/** `null` marks a candidate whose eligibility could not be read — shown as
 *  its own state rather than silently dropped, unlike `contactsFor` in
 *  `sourcing.ts` where a missing name can fall back to the id. There is no
 *  id-shaped fallback for a compliance flag: saying nothing here reads as
 *  "nothing to flag", which is the one thing it must never imply. */
export async function eligibilityFor(
  opportunityId: string,
  ids: string[],
  known: ReadonlyMap<string, Eligibility | null>,
): Promise<Map<string, Eligibility | null>> {
  const wanted = ids.filter((id) => !known.has(id));
  const found = new Map(known);
  await Promise.all(
    wanted.map(async (id) => {
      try {
        found.set(id, await getEligibility(opportunityId, id));
      } catch {
        found.set(id, null);
      }
    }),
  );
  return found;
}
