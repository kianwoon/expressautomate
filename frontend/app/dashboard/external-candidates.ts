"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  externalCandidateLatestPath,
  externalCandidateSearchPath,
  externalCandidateSearchResultsPath,
  identityResolutionPath,
  identityResolutionsPath,
  EXTERNAL_SEARCH_POLL_MS,
} from "../api";
import { ApiError, readError } from "./candidates";

/**
 * The one place that talks to the External Candidates endpoints.
 *
 * The search runs on the career bot, minutes long by its own design (spec §3:
 * poll every ~5s), and our API proxies it: POST starts the search and answers
 * 202 with a `task_id`; GET …/{task_id} reads the task's status; GET
 * …/{task_id}/results reads the ranked list. GET …/latest reads the newest
 * SAVED search — the career bot expires task results within hours, so the
 * backend keeps them on the search row and a returning visitor reloads them
 * from us. The hook mirrors `useJobIntelligence`: one `start()`, one polling
 * loop, one result, owned in one place and keyed on the opportunity id so it
 * follows the modal's remount-on-row-change lifecycle. On mount it loads the
 * saved search: finished results render immediately; a task still in flight
 * resumes the poll where the last tab left it.
 *
 * Everything goes out with `credentials: "include"` and comes back through
 * `readError`, so a refusal the server worded reaches the recruiter in the
 * server's words rather than ours.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

/** The task states the career bot reports (spec §3). `paused` means a human
 *  must take over on the service side — the spec says stop, not retry, so it
 *  is surfaced as its own sentence rather than folded into "failed". */
export type ExternalTaskStatus =
  | "pending"
  | "running"
  | "paused"
  | "waiting_approval"
  | "completed"
  | "failed";

/** The POST answer — `status` names what happened, and only `started` carries
 *  a task id. `unconfigured`/`unreachable`/`refused`/`not_provisioned` are the
 *  structured, never-a-500 answers the route gives; `message` is the sentence
 *  to show. */
export type ExternalSearchStart =
  | { status: "started"; task_id: string; message: null }
  | {
      status:
        | "unconfigured"
        | "unreachable"
        | "refused"
        | "not_provisioned";
      task_id: null;
      message: string;
      retry_after_seconds?: number;
    };

/** The poll answer. */
export type ExternalSearchStatus = {
  status: string;
  task_status: ExternalTaskStatus | null;
  error: string | null;
  message: string | null;
};

/** One ranked result, as the career bot defines it (spec §4). Passed through
 *  mostly untouched: the shape is the product, and mirroring every field
 *  would be a second copy of the spec that drifts. The fields the panel
 *  renders are named; anything else still travels inside the object. */
export type ExternalCandidate = {
  id: string;
  title: string;
  subtitle: string | null;
  location: string | null;
  source: string;
  source_platform: string | null;
  source_url: string | null;
  match_score: number;
  match_reason: string | null;
  gaps: unknown[];
  recommended_action: string | null;
  status: string;
  summary: string | null;
  skills: string[];
  credibility: {
    score: number;
    title_inflation: number;
    tenure_depth: number;
    evidence_ratio: number;
    flags: string[];
  } | null;
  [key: string]: unknown;
};

export type ExternalSearchResults = {
  status: string;
  task_id: string;
  task_status: ExternalTaskStatus | null;
  summary: string | null;
  results: ExternalCandidate[];
  message: string | null;
};

/** The newest SAVED search, as GET …/latest answers it — the row's own facts
 *  plus `results` as the career bot's verbatim ranked list. `task_status` is
 *  our reading of the row (`running` while unfinished, the terminal state it
 *  finished with otherwise), not a live poll of the career bot. */
export type ExternalSearchSaved = {
  task_id: string;
  task_status: ExternalTaskStatus | null;
  results: ExternalCandidate[];
  finished_at: string | null;
  created_at: string | null;
};

/** The user-facing head of the career bot's `summary`. The service returns
 *  the human line, then " — ", then the operator-facing plan log (spec §3:
 *  `plan_detail` is ours to read, not the recruiter's), so everything after
 *  the first " — " is trimmed. No " — " means the summary is already just
 *  the head; null/empty — or a head that is itself empty — yields null. */
export function summaryLine(summary: string | null): string | null {
  if (!summary) return null;
  const head = summary.split(" — ")[0].trim();
  return head || null;
}

/** The platform name a recruiter recognises for one result. The career bot
 *  now ships `source_platform` — its own display label (`LinkedIn`,
 *  `JobStreet`, …) — and that is the truth; this fallback only covers rows
 *  persisted before that field existed, where all we have is the raw source
 *  id (`linkedin_people`). The same ids the career bot itself labels
 *  (`matching.py`'s `_PLATFORM_LABELS`), so the two sides agree; an unknown
 *  id falls back to title-casing rather than a wrong platform name. */
export function platformLabel(
  candidate: Pick<ExternalCandidate, "source" | "source_platform"> | null | undefined,
): string | null {
  if (!candidate) return null;
  const shipped = candidate.source_platform;
  if (typeof shipped === "string" && shipped.trim()) return shipped.trim();
  const source = candidate.source;
  if (!source) return null;
  const key = source.trim().toLowerCase();
  if (!key) return null;
  if (key.includes("linkedin")) return "LinkedIn";
  if (key.includes("jobstreet")) return "JobStreet";
  if (key.includes("mycareersfuture")) return "MyCareersFuture";
  if (key.includes("fastjobs")) return "FastJobs";
  return source
    .trim()
    .split(/[\s_-]+/)
    .map((word) => (word ? word[0].toUpperCase() + word.slice(1) : word))
    .join(" ");

}

/** Read the newest saved search for a job order. `status: "none"` means no
 *  search has ever run — the state of the tab, not an error. */
export async function getLatestExternalSearch(
  opportunityId: string,
): Promise<{ status: string; search: ExternalSearchSaved | null; message: string | null }> {
  const res = await fetch(externalCandidateLatestPath(opportunityId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as {
    status: string;
    search: ExternalSearchSaved | null;
    message: string | null;
  };
}

/** Start one search. 202 with `status: "started"` and a task id — the answer
 *  arrives by polling, not in this response. The structured refusals
 *  (`unconfigured` / `unreachable` / `refused`) are ordinary answers here,
 *  not exceptions: the panel renders `message` either way, and the caller
 *  branches on `status` rather than on copy. */
export async function startExternalSearch(
  opportunityId: string,
): Promise<ExternalSearchStart> {
  const res = await fetch(externalCandidateSearchPath(opportunityId), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as ExternalSearchStart;
}

/** Read a task's status. */
export async function getExternalSearchStatus(
  opportunityId: string,
  taskId: string,
): Promise<ExternalSearchStatus> {
  const res = await fetch(
    externalCandidateSearchPath(opportunityId, taskId),
    { credentials: "include", headers: { Accept: "application/json" } },
  );
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as ExternalSearchStatus;
}

/** Read the ranked results. 404 here means "not visible to you" — the
 *  membership check happens server-side before the career bot is asked. */
export async function getExternalSearchResults(
  opportunityId: string,
  taskId: string,
): Promise<ExternalSearchResults> {
  const res = await fetch(
    externalCandidateSearchResultsPath(opportunityId, taskId),
    { credentials: "include", headers: { Accept: "application/json" } },
  );
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as ExternalSearchResults;
}

/** The task statuses worth asking again about (spec §3: poll every ~5s). */
export function externalSearchInFlight(status: ExternalTaskStatus | null): boolean {
  return status === "pending" || status === "running" || status === "waiting_approval";
}

/** The summary line for a SAVED search. The career bot's own summary ("10
 *  ranked results") is not stored, so the line is derived from what is: the
 *  count when results survived, otherwise a sentence that says the search
 *  finished without them. */
function savedSummary(saved: ExternalSearchSaved): string | null {
  if (saved.results.length > 0) {
    return `${saved.results.length} saved result${saved.results.length === 1 ? "" : "s"} from the last search.`;
  }
  return null;
}

/**
 * The external-search state for one job order, owned in one place — the same
 * split as `useJobIntelligence`: the hook owns the search, the modal owns the
 * layout. Keyed on the opportunity id; resetting when the row changes is the
 * caller's `key={row.id}` remount, not this hook's business.
 */
export function useExternalCandidates(rowId: string): {
  starting: boolean;
  startError: string | null;
  taskId: string | null;
  taskStatus: ExternalTaskStatus | null;
  taskError: string | null;
  results: ExternalSearchResults | null;
  resultsError: string | null;
  start: () => Promise<void>;
  reset: () => void;
} {
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskStatus, setTaskStatus] = useState<ExternalTaskStatus | null>(null);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [results, setResults] = useState<ExternalSearchResults | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  // Results are fetched once per (rowId, taskId), when the task completes.
  // The claim is keyed and released in the effect's cleanup, so a teardown +
  // re-run while the fetch is in flight (StrictMode double-invoke, a poll
  // re-render) refetches instead of swallowing the response.
  const fetchedFor = useRef<string | null>(null);

  const reset = useCallback(() => {
    setTaskId(null);
    setTaskStatus(null);
    setTaskError(null);
    setResults(null);
    setResultsError(null);
    setStartError(null);
  }, []);

  // On mount, load the newest SAVED search. Finished results render at once
  // — opening the job order again shows the candidates the last search
  // found, even though the career bot has long since expired the task. A
  // saved search still in flight resumes the poll through the per-task
  // routes, the same state a mid-search tab reload must land in. A load
  // failure stays quiet: the tab simply reads "no search yet", and Find
  // works exactly as it did before this fetch existed.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await getLatestExternalSearch(rowId);
        if (cancelled || body.status !== "ok" || !body.search) return;
        const saved = body.search;
        if (saved.task_status === "running") {
          setTaskId(saved.task_id);
          setTaskStatus("running");
          return;
        }
        setResults({
          status: "ok",
          task_id: saved.task_id,
          task_status: saved.task_status,
          summary: savedSummary(saved),
          results: saved.results,
          message: null,
        });
      } catch {
        // No saved search readable — the tab starts empty either way.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [rowId]);

  const start = useCallback(async () => {
    setStarting(true);
    setStartError(null);
    setTaskError(null);
    setResults(null);
    setResultsError(null);
    setTaskId(null);
    setTaskStatus(null);
    try {
      const started = await startExternalSearch(rowId);
      if (started.status === "started") {
        setTaskId(started.task_id);
        setTaskStatus("pending");
      } else {
        setStartError(started.message);
      }
    } catch (err) {
      setStartError(
        err instanceof Error ? err.message : "The search could not start just now.",
      );
    } finally {
      setStarting(false);
    }
  }, [rowId]);

  // The poll: only while the task is still working. `paused`/`failed`/
  // `completed` are terminal — the spec says a paused task needs a human on
  // the service side, so re-asking is noise.
  useEffect(() => {
    if (!taskId || !externalSearchInFlight(taskStatus)) return;
    let cancelled = false;
    const timer = setInterval(async () => {
      try {
        const status = await getExternalSearchStatus(rowId, taskId);
        if (cancelled) return;
        setTaskStatus(status.task_status);
        if (status.task_status === "failed" && status.error) {
          setTaskError(status.error);
        }
      } catch {
        // One lost poll is not a failed search — the next tick asks again.
      }
    }, EXTERNAL_SEARCH_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [rowId, taskId, taskStatus]);

  // Results, fetched once when the task completes. The claim is per
  // (rowId, taskId): a re-run before the fetch resolves must retry, not
  // early-return while the cancelled first run drops its response — that
  // race left a completed search showing a blank tab until remount.
  useEffect(() => {
    if (!taskId || taskStatus !== "completed") return;
    const claim = `${rowId}:${taskId}`;
    if (fetchedFor.current === claim) return;
    fetchedFor.current = claim;
    let cancelled = false;
    (async () => {
      try {
        const body = await getExternalSearchResults(rowId, taskId);
        if (!cancelled) setResults(body);
      } catch (err) {
        if (!cancelled) {
          setResultsError(
            err instanceof Error
              ? err.message
              : "The results could not be read just now.",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
      if (fetchedFor.current === claim) fetchedFor.current = null;
    };
  }, [rowId, taskId, taskStatus]);

  return {
    starting,
    startError,
    taskId,
    taskStatus,
    taskError,
    results,
    resultsError,
    start,
    reset,
  };
}

/**
 * The Candidate Identity Resolver, as the panel sees it (spec: "serper
 * design.md" §24, §27, §28, §57). One external candidate can be resolved to a
 * public professional identity; the result is stored server-side and can be
 * reopened from the job order's resolution history without re-running Serper.
 *
 * The hook mirrors `useExternalCandidates`: it owns the resolve call, the
 * per-candidate in-flight state, and the history list, while the panel owns
 * the layout. Kept deliberately small — Phase 1 adds a button, a badge, and a
 * modal; the asynchronous/batch flow is Phase 2 (§60).
 */

/** One evidence item, §13/§15 — the weighted signal and the page it came
 *  from. `type` is the spec's own vocabulary; the panel renders the server's
 *  sentence for `value`, not a translation of it. */
export type IdentityEvidenceItem = {
  type: string;
  value: string;
  source_url: string;
  source_domain: string;
  query: string;
  confidence: number;
  weight: number;
};

/** A resolved identity (§24/§57). `status` is `resolved` | `probable` |
 *  `unresolved`; `cached` is true when the server answered from its §34 cache
 *  rather than calling Serper again. */
export type ResolvedIdentity = {
  id: string;
  candidate_key: string;
  status: "resolved" | "probable" | "unresolved";
  confidence: number;
  canonical_profile_url: string | null;
  current_company: string | null;
  current_title: string | null;
  location: string | null;
  previous_companies: string[];
  evidence: IdentityEvidenceItem[];
  queries_used: number;
  freshness_status: "current" | "possible_change" | "unknown" | null;
  created_at: string | null;
  expires_at: string | null;
  cached?: boolean;
};

/** A row of the history list — enough to reopen the modal by id. */
export type IdentityResolutionSummary = {
  id: string;
  candidate_key: string;
  status: ResolvedIdentity["status"];
  confidence: number;
  created_at: string | null;
  expires_at: string | null;
  expired: boolean;
};

/** §56 mode. `refresh`/`deep` deliberately bypass the §34 cache. */
export type IdentityResolveMode = "normal" | "refresh" | "deep";

/** What the POST needs from a candidate — the §6 fingerprint fields, taken
 *  from the candidate object the panel already has.
 *
 *  The career bot's result shape has no dedicated employer field, so the
 *  fingerprint is assembled from what it does carry, conservatively: an
 *  explicit `company`/`employer` field when a future version ships one, else
 *  the "Title at Company" split of `subtitle`, and `location` verbatim. A
 *  wrong value here poisons the whole resolution (§52: employer and location
 *  carry the weight), so nothing is guessed beyond that one split. */
export function identityFingerprint(candidate: ExternalCandidate): {
  name: string;
  current_company: string | null;
  current_title: string | null;
  location: string | null;
  previous_companies: string[];
} {
  const record = candidate as Record<string, unknown>;
  const explicitCompany =
    typeof record.company === "string"
      ? record.company
      : typeof record.employer === "string"
        ? record.employer
        : null;
  const subtitle = candidate.subtitle ?? null;
  // "Product Control at Standard Chartered" → title / employer. The career
  // bot's own subtitle convention; anything without the marker stays a title.
  const at = subtitle ? subtitle.split(/\s+at\s+/i) : [];
  const parsedTitle = at.length === 2 ? at[0].trim() : subtitle;
  const parsedCompany = at.length === 2 ? at[1].trim() : null;
  return {
    name: candidate.title,
    current_company: explicitCompany ?? parsedCompany,
    current_title: parsedTitle,
    location: candidate.location ?? null,
    previous_companies: [],
  };
}

/** Resolve one candidate. The structured statuses (`unconfigured` /
 *  `unreachable` / `not_provisioned`) are ordinary answers here, not
 *  exceptions — the panel branches on `status`, never on copy. A cache hit is
 *  a normal 200 with `cached: true`. */
export async function resolveIdentity(
  opportunityId: string,
  candidate: ExternalCandidate,
  mode: IdentityResolveMode = "normal",
): Promise<ResolvedIdentity> {
  const res = await fetch(identityResolutionPath(opportunityId, candidate.id), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ mode, candidate: identityFingerprint(candidate) }),
  });
  if (!res.ok) throw new ApiError(await readError(res));
  return (await res.json()) as ResolvedIdentity;
}

/** The job order's resolution history, newest first — what the past-results
 *  list in the modal reads. */
export async function listIdentityResolutions(
  opportunityId: string,
): Promise<IdentityResolutionSummary[]> {
  const res = await fetch(identityResolutionsPath(opportunityId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  const body = (await res.json()) as { resolutions: IdentityResolutionSummary[] };
  return body.resolutions ?? [];
}

/** Reopen one stored resolution — a plain read, so no Serper call (§34). */
export async function getIdentityResolution(
  opportunityId: string,
  resolutionId: string,
): Promise<ResolvedIdentity> {
  const res = await fetch(identityResolutionsPath(opportunityId, resolutionId), {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new ApiError(await readError(res));
  const body = (await res.json()) as { resolution: ResolvedIdentity };
  return body.resolution;
}

export function useIdentityResolution(opportunityId: string): {
  resolvingFor: string | null;
  error: string | null;
  results: Record<string, ResolvedIdentity>;
  history: IdentityResolutionSummary[];
  resolve: (
    candidate: ExternalCandidate,
    mode?: IdentityResolveMode,
  ) => Promise<void>;
  reopen: (resolutionId: string) => Promise<void>;
  refreshHistory: () => Promise<void>;
} {
  const [resolvingFor, setResolvingFor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, ResolvedIdentity>>({});
  const [history, setHistory] = useState<IdentityResolutionSummary[]>([]);

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await listIdentityResolutions(opportunityId));
    } catch {
      // History is a convenience; its absence must not error the panel.
    }
  }, [opportunityId]);

  useEffect(() => {
    void refreshHistory();
  }, [refreshHistory]);

  const resolve = useCallback(
    async (candidate: ExternalCandidate, mode: IdentityResolveMode = "normal") => {
      setResolvingFor(candidate.id);
      setError(null);
      try {
        const body = await resolveIdentity(opportunityId, candidate, mode);
        setResults((prev) => ({ ...prev, [candidate.id]: body }));
        await refreshHistory();
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : "The identity could not be resolved just now.",
        );
      } finally {
        setResolvingFor(null);
      }
    },
    [opportunityId, refreshHistory],
  );

  const reopen = useCallback(
    async (resolutionId: string) => {
      setError(null);
      try {
        const body = await getIdentityResolution(opportunityId, resolutionId);
        setResults((prev) => ({ ...prev, [body.candidate_key]: body }));
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : "That saved result could not be opened.",
        );
      }
    },
    [opportunityId],
  );

  return { resolvingFor, error, results, history, resolve, reopen, refreshHistory };
}
