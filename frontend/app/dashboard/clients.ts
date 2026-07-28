"use client";

import { useCallback, useEffect, useState } from "react";

import {
  CLIENTS_PATH,
  clientArchivePath,
  clientConfirmPath,
  clientMergePath,
  clientPath,
  clientRestorePath,
  clientUnmergePath,
} from "../api";

/**
 * The agency's client list, and the one place that talks to the clients
 * endpoint.
 *
 * Every row was proposed by the ingestion pipeline, never typed by a person —
 * the opposite of `candidates.ts`. So there is no create and no delete here:
 * only the state transitions a human makes over a proposal (confirm, archive,
 * restore, merge, unmerge). Follows the same fetch and typing pattern as
 * `candidates.ts`: `credentials: "include"`, an `Accept: application/json`
 * header, and `URLSearchParams` for the query string.
 */

export type ClientStatus = "unconfirmed" | "confirmed" | "archived" | "merged";

// "human" is not included: `client_matching.py` only ever writes
// "email_domain" or "name" (checked at `_resolve` and its callers) — there is
// no code path that records a manual match.
export type MatchedBy = "email_domain" | "name";

export type ClientMention = {
  id: string;
  email_message_id: string | null;
  matched_by: MatchedBy;
  created_at: string;
};

export type Client = {
  id: string;
  name: string;
  name_normalized: string;
  email_domain: string | null;
  status: ClientStatus;
  merged_into_client_id: string | null;
  last_seen_at: string | null;
  created_at: string;
  /** Only present on the single-record GET, not on a list row. */
  mentions?: ClientMention[];
};

export type ClientPage = {
  items: Client[];
  total: number;
  limit: number;
  offset: number;
  counts: Record<string, number>;
};

/** The chips. `null` is "All" (every non-merged row, the review queue plus
 *  everything already confirmed or archived). `"merged"` is reached only by
 *  explicit filter — merged rows are excluded from the default list and the
 *  pointer runs loser -> survivor with no link back, so without this chip a
 *  wrongly merged client could never be found again to unmerge. */
export type Filter = null | ClientStatus;

function listUrl(filter: Filter, offset: number): string {
  const params = new URLSearchParams({ limit: "50", offset: String(offset) });
  if (filter) params.set("status", filter);
  return `${CLIENTS_PATH}?${params.toString()}`;
}

function messageFor(status: number): string {
  // A 401 is our session expiring, not the client list failing. Saying "we
  // could not load your clients" for an expired cookie sends someone to look
  // at the wrong thing entirely.
  return status === 401
    ? "Your session has expired. Sign in again to see your clients."
    : "We could not load your clients just now.";
}

export type ListState =
  | { status: "loading" }
  | { status: "ready"; page: ClientPage }
  | { status: "unreadable"; message: string };

const ZERO_COUNTS: Record<string, number> = { all: 0 };

export type Clients = {
  state: ListState;
  filter: Filter;
  offset: number;
  /** The last counts we were told, kept across a reload so the chips do not
   *  blink back to nothing every time a filter changes. */
  counts: Record<string, number>;
  setFilter: (filter: Filter) => void;
  setOffset: (offset: number) => void;
  reload: () => void;
};

/** The review queue is the centre of gravity here: the default view is the
 *  unconfirmed proposals a recruiter has not yet judged, not the whole
 *  tenant mixed together. */
export function useClients(): Clients {
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [filter, setFilterRaw] = useState<Filter>("unconfirmed");
  const [offset, setOffset] = useState(0);
  const [counts, setCounts] = useState<Record<string, number>>(ZERO_COUNTS);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    (async () => {
      try {
        const res = await fetch(listUrl(filter, offset), {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) {
          setState({ status: "unreadable", message: messageFor(res.status) });
          return;
        }
        const page = (await res.json()) as ClientPage;
        setState({ status: "ready", page });
        setCounts(page.counts);
      } catch {
        if (!controller.signal.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
        }
      }
    })();
    return () => controller.abort();
  }, [filter, offset, nonce]);

  const setFilter = useCallback((next: Filter) => {
    setFilterRaw(next);
    setOffset(0);
  }, []);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { state, filter, offset, counts, setFilter, setOffset, reload };
}

/** Turns a non-2xx response into the server's own message where it gave one,
 *  rather than a generic failure. A 409 on merge or unmerge names the
 *  conflicting client — the message is the only thing telling the recruiter
 *  what to do next. */
async function readError(res: Response): Promise<string> {
  if (res.status === 401) return "Your session has expired. Sign in again, then try that once more.";
  try {
    const body = (await res.json()) as { detail?: string };
    if (body.detail) return body.detail;
  } catch {
    /* not JSON, or empty */
  }
  return "We could not save that just now. Nothing has changed.";
}

export class ApiError extends Error {}

export async function getClient(id: string): Promise<Client> {
  const res = await fetch(clientPath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Client;
}

export async function confirmClient(id: string): Promise<void> {
  const res = await fetch(clientConfirmPath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function archiveClient(id: string): Promise<void> {
  const res = await fetch(clientArchivePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function restoreClient(id: string): Promise<void> {
  const res = await fetch(clientRestorePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function mergeClient(id: string, targetId: string): Promise<void> {
  const res = await fetch(clientMergePath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ target_id: targetId }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function unmergeClient(id: string): Promise<void> {
  const res = await fetch(clientUnmergePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}
