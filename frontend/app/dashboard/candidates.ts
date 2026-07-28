"use client";

import { useCallback, useEffect, useState } from "react";

import {
  CANDIDATES_PATH,
  candidateArchivePath,
  candidateAvatarPath,
  candidateMergePath,
  candidatePath,
  candidateRestorePath,
  candidateUnmergePath,
} from "../api";

/**
 * The agency's candidate list, and the one place that talks to the
 * candidates endpoint.
 *
 * Nothing here is AI-derived (§ candidate matching design) — every value was
 * typed by a person or came from a spreadsheet a person uploaded, so there is
 * no confidence and no evidence, only the record and who last edited it.
 *
 * Follows the fetch and typing pattern in `opportunities.ts`: `credentials:
 * "include"`, an `Accept: application/json` header, and `URLSearchParams` for
 * the query string.
 */

export type Stage = "new" | "contacted" | "submitted" | "placed" | "rejected";

export type Candidate = {
  id: string;
  full_name: string;
  email: string | null;
  phone_raw: string | null;
  current_title: string | null;
  current_employer: string | null;
  location: string | null;
  years_experience: number | null;
  expected_salary: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  available_from: string | null;
  notice_period_raw: string | null;
  employment_type: string | null;
  notes: string | null;
  pipeline_stage: Stage;
  record_status: "active" | "archived" | "merged";
  updated_at: string;
  merged_into_candidate_id?: string | null;
  /** Only present on the single-record GET, not on a list row. */
  skills?: string[];
  /** Only present on the single-record GET, not on a list row. */
  overridden_fields?: string[];
  /** Set once a photo has been uploaded; `null` means "show the initials
   *  fallback" rather than "loading". Never used to build a URL directly —
   *  the URL itself is a separate, short-lived fetch (see `getCandidateAvatar`). */
  avatar_key: string | null;
  avatar_updated_at: string | null;
};

export type CandidatePage = {
  items: Candidate[];
  total: number;
  limit: number;
  offset: number;
  counts: Record<string, number>;
};

/** The chips. `null` is "All" (every non-merged record). `"merged"` is its
 *  own value rather than a `Stage` — merged rows have no pipeline stage
 *  worth filtering on, and reaching them is a `record_status` query, not a
 *  `pipeline_stage` one. */
export type Filter = null | Stage | "merged";

function listUrl(filter: Filter, offset: number, q: string): string {
  const params = new URLSearchParams({ limit: "50", offset: String(offset) });
  if (filter === "merged") params.set("record_status", "merged");
  else if (filter) params.set("pipeline_stage", filter);
  if (q.trim()) params.set("q", q.trim());
  return `${CANDIDATES_PATH}?${params.toString()}`;
}

function messageFor(status: number): string {
  // A 401 is our session expiring, not the candidate list failing. Saying "we
  // could not load your candidates" for an expired cookie sends someone to
  // look at the wrong thing entirely.
  return status === 401
    ? "Your session has expired. Sign in again to see your candidates."
    : "We could not load your candidates just now.";
}

export type ListState =
  | { status: "loading" }
  | { status: "ready"; page: CandidatePage }
  | { status: "unreadable"; message: string };

const ZERO_COUNTS: Record<string, number> = { all: 0 };

export type Candidates = {
  state: ListState;
  filter: Filter;
  offset: number;
  q: string;
  /** The last counts we were told, kept across a reload so the chips do not
   *  blink back to nothing every time a filter changes. */
  counts: Record<string, number>;
  setFilter: (filter: Filter) => void;
  setOffset: (offset: number) => void;
  setQ: (q: string) => void;
  reload: () => void;
};

export function useCandidates(): Candidates {
  const [state, setState] = useState<ListState>({ status: "loading" });
  const [filter, setFilterRaw] = useState<Filter>(null);
  const [offset, setOffset] = useState(0);
  const [q, setQRaw] = useState("");
  const [counts, setCounts] = useState<Record<string, number>>(ZERO_COUNTS);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    (async () => {
      try {
        const res = await fetch(listUrl(filter, offset, q), {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) {
          setState({ status: "unreadable", message: messageFor(res.status) });
          return;
        }
        const page = (await res.json()) as CandidatePage;
        setState({ status: "ready", page });
        setCounts(page.counts);
      } catch {
        if (!controller.signal.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
        }
      }
    })();
    return () => controller.abort();
  }, [filter, offset, q, nonce]);

  // Changing the filter or the search must reset the page, for the same
  // reason as job orders: staying on offset 150 of five matching rows reads
  // exactly like "there are none".
  const setFilter = useCallback((next: Filter) => {
    setFilterRaw(next);
    setOffset(0);
  }, []);
  const setQ = useCallback((next: string) => {
    setQRaw(next);
    setOffset(0);
  }, []);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { state, filter, offset, q, counts, setFilter, setOffset, setQ, reload };
}

/** Turns a non-2xx response into the server's own message where it gave one,
 *  rather than a generic failure. A 409 here is a split identity or a key
 *  someone else already holds — the message is the only thing telling the
 *  recruiter what to do next. */
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

export async function getCandidate(id: string): Promise<Candidate> {
  const res = await fetch(candidatePath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Candidate;
}

export async function createCandidate(
  body: Partial<Candidate> & { full_name: string },
): Promise<Candidate> {
  const res = await fetch(CANDIDATES_PATH, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Candidate;
}

export async function updateCandidate(id: string, body: Partial<Candidate>): Promise<Candidate> {
  const res = await fetch(candidatePath(id), {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as Candidate;
}

export async function archiveCandidate(id: string): Promise<void> {
  const res = await fetch(candidateArchivePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function restoreCandidate(id: string): Promise<void> {
  const res = await fetch(candidateRestorePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function deleteCandidate(id: string): Promise<void> {
  const res = await fetch(candidatePath(id), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function mergeCandidate(id: string, targetId: string): Promise<void> {
  const res = await fetch(candidateMergePath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ target_id: targetId }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export async function unmergeCandidate(id: string): Promise<void> {
  const res = await fetch(candidateUnmergePath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export type AvatarUrl = { url: string; expires_in: number };

/** A presigned URL good for roughly `expires_in` seconds — `null` means the
 *  candidate has no photo (a 404), not a failed request. Callers must not
 *  hold onto the URL past the component's lifetime: it is re-fetched every
 *  time the panel opens rather than cached anywhere longer-lived. */
export async function getCandidateAvatar(id: string): Promise<AvatarUrl | null> {
  const res = await fetch(candidateAvatarPath(id), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as AvatarUrl;
}

export async function uploadCandidateAvatar(
  id: string,
  file: File,
): Promise<{ avatar_key: string; avatar_updated_at: string }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(candidateAvatarPath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
    body: form,
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as { avatar_key: string; avatar_updated_at: string };
}

export async function deleteCandidateAvatar(id: string): Promise<void> {
  const res = await fetch(candidateAvatarPath(id), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}
