"use client";

import { useEffect, useState } from "react";

import { MEMBERS_PATH } from "../api";

/** Someone who works at this agency, as the assignment and sharing controls
 *  need them. `name` is already resolved server-side (preferred name, then
 *  display name, then the email's local part), so nothing here re-derives it —
 *  two places deciding what someone is called is two places to disagree. */
export type Member = {
  id: string;
  name: string;
  email: string;
  role: string;
};

export type MembersState = {
  status: "loading" | "ready" | "unreadable";
  members: Member[];
  message?: string;
};

/** The agency's staff list.
 *
 * Cached at module scope rather than refetched per mount: the list changes
 * when somebody joins the agency, which is not on the timescale of a dialog
 * opening. `resetMembers()` exists for sign-out and for tests.
 *
 * The promise is cached, not the result, so two dialogs opening in the same
 * tick share one request rather than racing two. A rejected promise is dropped
 * rather than kept — a cached failure would leave the controls permanently
 * unreadable for the life of the tab over one flaky response.
 */
let cache: Promise<Member[]> | null = null;

export function resetMembers(): void {
  cache = null;
}

function loadMembers(): Promise<Member[]> {
  if (cache) return cache;
  const pending: Promise<Member[]> = (async () => {
    const response = await fetch(MEMBERS_PATH, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`Members request failed (${response.status})`);
    return (await response.json()) as Member[];
  })().catch((error) => {
    // Compared against `pending` — the promise actually stored — so a
    // `resetMembers()` that lands mid-flight is not undone by this.
    if (cache === pending) cache = null;
    throw error;
  });
  cache = pending;
  return pending;
}

export function useMembers(): MembersState {
  const [state, setState] = useState<MembersState>({ status: "loading", members: [] });

  useEffect(() => {
    let live = true;
    loadMembers().then(
      (members) => {
        if (live) setState({ status: "ready", members });
      },
      () => {
        // The message is deliberately about what the reader can do, not about
        // the status code: a picker that cannot list colleagues is a working
        // screen with one broken control, not an error page.
        if (live) {
          setState({
            status: "unreadable",
            members: [],
            message: "Couldn’t load your colleagues. Try again in a moment.",
          });
        }
      },
    );
    return () => {
      live = false;
    };
  }, []);

  return state;
}
