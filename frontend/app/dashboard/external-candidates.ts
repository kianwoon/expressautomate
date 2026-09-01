"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  externalCandidateSearchPath,
  externalCandidateSearchResultsPath,
  EXTERNAL_SEARCH_POLL_MS,
} from "../api";
import { ApiError, readError } from "./candidates";

/**
 * The one place that talks to the External Candidates endpoints.
 *
 * The search runs on the career bot, minutes long by its own design (spec §3:
 * poll every ~5s), and our API proxies it: POST starts the search and answers
 * 202 with a `task_id`; GET …/{task_id} reads the task's status; GET
 * …/{task_id}/results reads the ranked list. The hook mirrors
 * `useJobIntelligence`: one `start()`, one polling loop, one result, owned in
 * one place and keyed on the opportunity id so it follows the modal's
 * remount-on-row-change lifecycle.
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
  // Results are fetched once, when the task completes; the ref guards the
  // poll effect's cleanup race (the interval can fire once more after the
  // status flips terminal before the effect tears down).
  const fetchingResults = useRef(false);

  const reset = useCallback(() => {
    setTaskId(null);
    setTaskStatus(null);
    setTaskError(null);
    setResults(null);
    setResultsError(null);
    setStartError(null);
  }, []);

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

  // Results, fetched once when the task completes.
  useEffect(() => {
    if (!taskId || taskStatus !== "completed" || fetchingResults.current) return;
    let cancelled = false;
    fetchingResults.current = true;
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
      } finally {
        fetchingResults.current = false;
      }
    })();
    return () => {
      cancelled = true;
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
