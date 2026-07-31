"use client";

import {
  OPPORTUNITIES_PATH,
  opportunityAssignPath,
  opportunityClaimPath,
  opportunityClientPath,
} from "../api";

/**
 * The writes: claiming, assigning, and typing a job order in by hand.
 *
 * Split out of `opportunities.ts` when adding `createOpportunity` would have
 * pushed that file past the 1500-line ceiling's working threshold. The line
 * drawn here is not arbitrary: everything in this file is a one-shot write
 * that returns a result, while `opportunities.ts` is the list — its state, its
 * polling and its races. `opportunities.ts` re-exports all of this, so no
 * caller had to change and none should start importing from here directly
 * unless it is writing something new.
 */

/**
 * What a claim or an assignment did.
 *
 * A result rather than a thrown error, because none of these are exceptional:
 * two recruiters reaching for the same job order in the same second is an
 * ordinary Tuesday, and the caller's job is to render a sentence, not to
 * recover. `kind` is carried separately from the message so a screen can
 * recognise an outcome without parsing copy: re-wording a sentence, or
 * translating one, must never change what a panel does.
 *
 * A `kind` rather than the HTTP status, because two of the five outcomes have
 * no status at all — an unreachable server and an unrecognised code are both
 * `failed`, and inventing a 0 or a 500 for them would be a number nothing sent.
 */
export type MutationFailure = "conflict" | "gone" | "forbidden" | "denied" | "failed";

export type MutationResult =
  | { ok: true }
  | { ok: false; kind: MutationFailure; message: string };

/**
 * One sentence per status, and the statuses are the whole design.
 *
 * 409 is "someone else has taken this one" — the losing side of a real race,
 * which is a fact about the world rather than a fault. 404 is "no longer
 * available", worded so it says nothing about whether the row exists: the
 * server refuses to distinguish "gone" from "never yours", and repeating that
 * refusal here is what keeps one agency from probing another's ids. 403 is the
 * one case where the row is admittedly visible and still not yours to move.
 * Collapsing any of these into "something went wrong" throws away the only
 * thing the backend went to the trouble of telling us.
 *
 * allow-hardcode: user-facing copy keyed by HTTP status — the statuses are the
 * logic, these strings are only what the recruiter reads. Nothing is matched
 * against them.
 */
/** Status in, `kind` and sentence out. The `kind` is what a screen branches
 *  on — a 404 under an open panel is a closed state rather than an error, and
 *  a 403 is the one message a screen may replace with something truer about
 *  the row in front of it. The sentence beside it is only what the recruiter
 *  reads; nothing is ever compared against it. */
const MUTATION_OUTCOMES: Record<number, { kind: MutationFailure; message: string }> = {
  409: { kind: "conflict", message: "Someone else has taken this one." },
  404: { kind: "gone", message: "This job order is no longer available." },
  403: { kind: "forbidden", message: "This job order is not yours to reassign." },
  401: {
    kind: "denied",
    message: "Your session has expired. Sign in again, then try that once more.",
  },
};

const MUTATION_FALLBACK: { kind: MutationFailure; message: string } = {
  kind: "failed",
  message: "We could not save that just now. Nothing has changed.",
};

async function mutate(url: string, body?: unknown): Promise<MutationResult> {
  try {
    const res = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (res.ok) return { ok: true };
    // Only a 409 is a race. A dropped connection is not one, and reporting it
    // as one would tell a recruiter a colleague took the job order when nobody
    // did — so anything unrecognised is plainly `failed`.
    return { ok: false, ...(MUTATION_OUTCOMES[res.status] ?? MUTATION_FALLBACK) };
  } catch {
    return {
      ok: false,
      kind: "failed",
      message: "We could not reach the server. Nothing has changed.",
    };
  }
}

/** Takes an unassigned job order for yourself. */
export function claimOpportunity(id: string): Promise<MutationResult> {
  return mutate(opportunityClaimPath(id));
}

/** Hands a job order to a colleague, or — with `null` — releases it back to
 *  the unassigned queue, which is a state a recruiter chooses rather than a
 *  missing value. */
export function assignOpportunity(id: string, userId: string | null): Promise<MutationResult> {
  return mutate(opportunityAssignPath(id), { user_id: userId });
}

/**
 * Files a job order under the client it came from, or — with `null` — unfiles
 * it, which is the ordinary state of a company nobody has recorded yet rather
 * than a missing value.
 *
 * `adopt` asks the server to hand an *unassigned* job order to whoever already
 * looks after that client. It is a request, not an instruction: an already
 * assigned job order never changes hands, and a client with no recruiter has
 * nobody to hand it to. Which is why the caller reads the row back afterwards
 * instead of assuming — the server is the only place that knows whether the
 * owner moved, and inventing an answer here would be the second place deciding
 * who is doing the work.
 *
 * The response body is deliberately not returned. Everything it carries —
 * `client_id`, `assigned_user_id`, `assignee_name` — is on the row the caller
 * re-reads anyway, and a second, thinner copy of the same three fields is a
 * second thing that can disagree with the list.
 */
export function setOpportunityClient(
  id: string,
  clientId: string | null,
  adopt: boolean,
): Promise<MutationResult> {
  return mutate(opportunityClientPath(id), {
    client_id: clientId,
    adopt_client_recruiter: adopt,
  });
}
/**
 * A job order that never arrived as an email.
 *
 * Every field is a raw string because the recruiter is transcribing what they
 * were told: turning "6k neg." into a number would be the fabrication §15
 * forbids, and normalisation is the extraction pipeline's job. `client_id` is
 * nullable for the ordinary case of a company nobody has recorded yet.
 *
 * There is no `assigned_user_id`, and its absence is the design. The server
 * assigns the row to whoever created it — you typed it in, so it is yours,
 * not the client account holder's. Sending one from here would be a second
 * place deciding that, and the one most likely to get it wrong.
 */
export type ManualOpportunity = {
  client_id: string | null;
  company_name_raw: string | null;
  job_title_raw: string | null;
  location_raw: string | null;
  salary_raw: string | null;
  working_hours_raw: string | null;
  duration_raw: string | null;
  employment_type: string | null;
  job_description: string | null;
  requirements: string | null;
};

/** What the create endpoint echoes back: enough to identify the new row, not
 *  the whole record. The caller reads the row back if it needs all of it. */
export type CreatedOpportunity = { id: string };

/**
 * Creates one, and reports the same `kind`s every other write here does.
 *
 * A result rather than a throw, for the reason given above: a refused write is
 * a sentence the form shows beside the button, and the typed-in text stays
 * where it is so nothing has to be typed twice.
 */
export async function createOpportunity(
  body: ManualOpportunity,
): Promise<
  { ok: true; created: CreatedOpportunity } | { ok: false; kind: MutationFailure; message: string }
> {
  try {
    const res = await fetch(OPPORTUNITIES_PATH, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) return { ok: false, ...(MUTATION_OUTCOMES[res.status] ?? MUTATION_FALLBACK) };
    return { ok: true, created: (await res.json()) as CreatedOpportunity };
  } catch {
    return {
      ok: false,
      kind: "failed",
      message: "We could not reach the server. Nothing has changed.",
    };
  }
}
