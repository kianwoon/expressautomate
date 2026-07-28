"use client";

import { useCallback, useEffect, useState } from "react";

import {
  CANDIDATES_PATH,
  candidateArchivePath,
  candidateAvatarPath,
  candidateDocumentDownloadPath,
  candidateDocumentPath,
  candidateDocumentsPath,
  candidateMergePath,
  candidatePath,
  candidateRestorePath,
  candidateRoleConfirmPath,
  candidateRolePath,
  candidateRoleRejectPath,
  candidateRolesPath,
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

/** How much of a date the source actually stated. A CV that says "2019" has
 *  no month in it, and rendering "1 Jan 2019" would assert a day nobody
 *  wrote down — the §15 rule this whole feature is built around. The
 *  precision travels with the date so the reader can only ever be shown what
 *  the source supports. */
export type DatePrecision = "year" | "month" | "day";

/** One job the candidate held.
 *
 * `ended_on: null` is what makes a role current, and `is_current` is the
 * server saying so rather than a second stored fact that could disagree with
 * the dates beside it.
 *
 * `source` and `status` have one useful value each today — a recruiter typed
 * it, and it is confirmed. They ship now because the CV parser that will
 * write `unconfirmed` rows is the next piece of work, and adding the columns
 * later would mean a migration and a redesign rather than an endpoint. */
export type CandidateRole = {
  id: string;
  employer: string;
  employer_normalized: string;
  title: string;
  title_normalized: string;
  started_on: string | null;
  started_precision: DatePrecision | null;
  ended_on: string | null;
  ended_precision: DatePrecision | null;
  employment_type: string | null;
  location: string | null;
  description: string | null;
  source: string;
  status: "unconfirmed" | "confirmed" | "rejected";
  is_current: boolean;
  /** The line of the CV the model quoted for this role. Optional rather than
   *  always-present: a role a recruiter typed by hand has no evidence row,
   *  and the panel falls back to offering the source file itself in that
   *  case. */
  evidence?: string | null;
};

/** How far the reading of one uploaded CV has got.
 *
 * These are not five flavours of failure and they must not read as one.
 * `unreadable` is permanent and has a cause a recruiter can act on — a scan
 * carries no text layer. `empty` is a file we read fine that yielded nothing
 * that survived checking. `failed` is transient and worth retrying. The
 * difference between them is the whole difference between a product that is
 * broken and one that is telling you what happened. */
export type ParseState = "pending" | "parsing" | "parsed" | "unreadable" | "empty" | "failed";

/** One CV uploaded against a candidate.
 *
 * `dropped_count` and `dropped_reason` stay set on a *successful* parse: they
 * are the note that says the model proposed things whose quoted text could not
 * be found in the document and they were thrown away. Without it a recruiter
 * whose CV lists five jobs and whose panel shows three concludes the parser
 * cannot count. */
export type CandidateDocument = {
  id: string;
  filename: string;
  content_type: string;
  byte_size: number;
  parse_state: ParseState;
  parse_error: string | null;
  text_chars: number | null;
  dropped_count: number | null;
  dropped_reason: string | null;
  created_at: string | null;
};

/** What a create sends, and — one key at a time — what a patch sends. */
export type CandidateRoleBody = {
  employer?: string;
  title?: string;
  started_on?: string | null;
  started_precision?: DatePrecision | null;
  ended_on?: string | null;
  ended_precision?: DatePrecision | null;
  employment_type?: string | null;
  location?: string | null;
  description?: string | null;
};

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
  /** Only present on the single-record GET, not on a list row. Optional
   *  rather than defaulted to `[]`, because a list row has not been asked
   *  about its roles — absent must read as "not loaded", never as "none".
   *  Already ordered current-first then newest by the server. */
  roles?: CandidateRole[];
  /** Only present on the single-record GET, not on a list row. Optional for
   *  the same reason `roles` is: a table row has never been asked about its
   *  uploads, and absent has to mean "not loaded" rather than "none" — the
   *  difference between saying nothing and asserting there is no CV.
   *  Newest first, as the server orders them. */
  documents?: CandidateDocument[];
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
  /** A refetch is in flight over rows we are still showing. The same reasoning
   *  as `counts`, applied to the table: a reload that dropped `state` back to
   *  `loading` would unmount the table and the open detail panel, so archiving
   *  a row or uploading a photo read as the whole page going blank. The
   *  in-flight fact is reported here instead, and only the genuine first load —
   *  when there is nothing on screen yet — is a `loading` state. */
  refreshing: boolean;
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
  const [refreshing, setRefreshing] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    // Rows already on screen stay on screen while the refetch runs, exactly as
    // the counts do — a page that is being re-read is not a page with nothing
    // in it, and collapsing to `loading` here is what made an avatar upload,
    // an archive or a merge look like the whole screen had reloaded.
    setState((prev) => (prev.status === "ready" ? prev : { status: "loading" }));
    setRefreshing(true);
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
      } finally {
        // An aborted request has been superseded by the next one, which has
        // already set this back to true; clearing it here would report idle
        // while a fetch is still running.
        if (!controller.signal.aborted) setRefreshing(false);
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

  return { state, filter, offset, q, counts, refreshing, setFilter, setOffset, setQ, reload };
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

export async function createCandidateRole(
  id: string,
  body: CandidateRoleBody & { employer: string; title: string },
): Promise<CandidateRole> {
  const res = await fetch(candidateRolesPath(id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateRole;
}

/** A true partial update: only the keys present in `body` are touched, and an
 *  explicit `null` clears a nullable field. Callers must therefore send the
 *  keys they mean and omit the rest — spreading a whole role in here would
 *  turn every edit into a replacement. */
export async function updateCandidateRole(
  id: string,
  roleId: string,
  body: CandidateRoleBody,
): Promise<CandidateRole> {
  const res = await fetch(candidateRolePath(id, roleId), {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateRole;
}

export async function deleteCandidateRole(id: string, roleId: string): Promise<void> {
  const res = await fetch(candidateRolePath(id, roleId), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

/** A person vouches for what the parse read off the CV. Returns the updated
 *  role; the caller still refetches the candidate, because confirming can move
 *  the derived title, employer and years above. */
export async function confirmCandidateRole(id: string, roleId: string): Promise<CandidateRole> {
  const res = await fetch(candidateRoleConfirmPath(id, roleId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateRole;
}

/** A person says the parse got this one wrong. The row is kept rather than
 *  deleted — it is the record of what the model claimed, and a re-parse of the
 *  same CV must not resurrect something already thrown out. */
export async function rejectCandidateRole(id: string, roleId: string): Promise<CandidateRole> {
  const res = await fetch(candidateRoleRejectPath(id, roleId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateRole;
}

/** Uploads a CV. 202, not 201: the file is stored and queued, and the reading
 *  of it happens afterwards — which is why the document comes back in a
 *  `pending` state rather than with roles attached. */
export async function uploadCandidateDocument(
  id: string,
  file: File,
): Promise<CandidateDocument> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(candidateDocumentsPath(id), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
    body: form,
  });
  // Oversized, wrong type and over-quota all arrive as a readable sentence the
  // server wrote; `readError` surfaces it rather than replacing it with a
  // guess about which of the three it was.
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as CandidateDocument;
}

export async function deleteCandidateDocument(id: string, documentId: string): Promise<void> {
  const res = await fetch(candidateDocumentPath(id, documentId), {
    method: "DELETE",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
}

export type DocumentUrl = { url: string; expires_in: number };

/** A short-lived presigned URL for the original file. Fetched at the moment
 *  the recruiter asks for it and never held: the link stops working within
 *  minutes, so anything longer-lived than the click is a broken link waiting
 *  to be found. */
export async function getCandidateDocumentUrl(
  id: string,
  documentId: string,
): Promise<DocumentUrl> {
  const res = await fetch(candidateDocumentDownloadPath(id, documentId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as DocumentUrl;
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
