"use client";

import { useCallback, useState } from "react";

/**
 * Rows-per-page is the one list preference recruiters asked to have
 * remembered across the candidates, clients and job orders screens —
 * everything else on those screens is meant to reset to a sane default on
 * every visit. Persisted per browser via localStorage; there is no
 * user-preferences table on the backend and this does not attempt to become
 * the start of one.
 *
 * Kept beside the hooks that use it rather than in `app/api.ts`: the
 * constants there describe the API — request paths, query params, the
 * server-facing page-size default and ladder. A localStorage key is a
 * browser-only display concern the server never sees, so it lives with the
 * other browser-only concern in this directory, `signed-url-cache.ts`.
 */

/** One constant per screen, not a single shared key — sharing one key would
 *  make picking 20 rows on candidates silently resize clients too, which is
 *  the opposite of "remembers what I chose here". */
export const CANDIDATES_PAGE_SIZE_KEY = "ea.pageSize.candidates";
export const CLIENTS_PAGE_SIZE_KEY = "ea.pageSize.clients";
export const OPPORTUNITIES_PAGE_SIZE_KEY = "ea.pageSize.jobOrders";
export const BUDDIES_PAGE_SIZE_KEY = "ea.pageSize.buddies";

/**
 * A page-size `useState` that reads its initial value from localStorage and
 * writes every change back to it, validated against that screen's ladder.
 *
 * `ladder` is passed in rather than assumed because it already differs per
 * screen (candidates and clients agree today, job orders is its own set) —
 * this hook does not decide what values are legal, only whether a stored one
 * still is.
 */
export function usePersistedPageSize(
  storageKey: string,
  fallback: number,
  ladder: readonly number[],
): [number, (next: number) => void] {
  // Read in a lazy initialiser rather than an effect, because the stored size
  // has to be known before the first fetch: an effect would fetch the default
  // page, then refetch the remembered one, which is a wasted request and a
  // visible flash of the wrong row count.
  //
  // The `typeof window` guard is for the prerender pass — `next export` runs
  // this under Node, where touching localStorage would throw and fail the
  // build. It is NOT about hydration: the pager holding the `<select>` lives
  // in the ready branch, and at build time these screens are still loading,
  // so no page size ever reaches the prerendered HTML to disagree with.
  const [size, setSize] = useState<number>(() => {
    if (typeof window === "undefined") return fallback;
    return readStoredPageSize(storageKey, fallback, ladder);
  });

  const setPersisted = useCallback(
    (next: number) => {
      setSize(next);
      if (typeof window === "undefined") return;
      try {
        window.localStorage.setItem(storageKey, String(next));
      } catch {
        // Safari private mode, a full quota, or storage disabled outright —
        // the control keeps working for the rest of this session, it just
        // stops remembering the choice past a reload.
      }
    },
    [storageKey],
  );

  return [size, setPersisted];
}

function readStoredPageSize(
  key: string,
  fallback: number,
  ladder: readonly number[],
): number {
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(key);
  } catch {
    return fallback;
  }
  if (raw === null) return fallback;
  const parsed = Number(raw);
  // localStorage is user-writable and outlives an env change: reject
  // anything that isn't a genuine, currently-offered page size — NaN, zero,
  // negative, or a value the current ladder no longer includes (an operator
  // changing NEXT_PUBLIC_*_PAGE_SIZE can leave an old stored value off the
  // ladder, and a <select> whose value isn't one of its own options renders
  // blank rather than falling back on its own).
  if (!Number.isFinite(parsed) || parsed <= 0 || !ladder.includes(parsed)) {
    return fallback;
  }
  return parsed;
}
