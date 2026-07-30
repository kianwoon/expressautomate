"use client";

import { opportunitySharePath, opportunitySharesPath } from "../api";
import { ApiError, readError } from "./candidates";

/**
 * Who else can see one job order.
 *
 * There is no access level here, and adding one would be inventing a concept
 * the server does not have: a share grants sight. The recipient reads the job
 * order and may hand it on to a colleague of their own — that chain is the
 * point, because the person who can fill a vacancy is usually one desk further
 * than whoever first saw the email. What they cannot do is edit it, and what
 * they cannot do is broadcast it, which is enforced server-side and mirrored
 * in the dialog rather than only hinted at.
 *
 * Two scopes, not two features. `user` names colleagues; `tenant` is the one
 * canonical broadcast row for the agency. The server returns 201 for the
 * broadcast and 200 for a named share — re-sharing updates the note rather
 * than colliding — so nothing here branches on the status code beyond ok.
 */

export type ShareScope = "user" | "tenant";

export type Share = {
  id: string;
  scope: ShareScope;
  /** Null exactly for the tenant broadcast, which is shared with everybody
   *  rather than with a particular person. */
  shared_with_user_id: string | null;
  shared_by_user_id: string | null;
  note: string | null;
  created_at: string;
};

export type ShareRequest = {
  scope: ShareScope;
  user_ids: string[];
  /** Trimmed to `null` by the caller: an empty string is a note that says
   *  nothing, and storing one would put a blank line in the notification. */
  note: string | null;
};

const JSON_HEADERS = { "Content-Type": "application/json", Accept: "application/json" };

/** Every share on this job order, oldest first — the server's order, kept. */
export async function listShares(id: string): Promise<Share[]> {
  const res = await fetch(opportunitySharesPath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  const body = (await res.json()) as { items?: Share[] };
  return body.items ?? [];
}

/** Hands this job order to named colleagues, or to the whole agency.
 *
 * Returns how many people were newly given sight of it — re-sharing to
 * somebody who already had it counts for nothing, because they were told the
 * first time and a second message says nothing new. */
export async function shareOpportunity(
  id: string,
  body: ShareRequest,
): Promise<{ newly_shared_with: number }> {
  const res = await fetch(opportunitySharesPath(id), {
    method: "POST",
    credentials: "include",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as { newly_shared_with: number };
}

/** Takes sight back. Deletes one share row and nothing else — no candidate,
 *  no note, no record of what the recipient did while they could see it. */
export async function unshare(id: string, shareId: string): Promise<void> {
  const res = await fetch(opportunitySharePath(id, shareId), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}
