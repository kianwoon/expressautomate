"use client";

import { useEffect, useState } from "react";

import { ME_PATH } from "./api";

export type Me = {
  user: { id: string; email: string; display_name: string | null; role: string };
  tenant: { id: string; name: string; is_personal_account: boolean };
  mailbox: {
    provider: string;
    connected: boolean;
    scopes: string[];
    ingestion_active: boolean;
  };
};

/**
 * Four states, deliberately. "unreachable" is kept apart from "anonymous":
 * a network failure or a 5xx must never be presented as being signed out,
 * because that would push a signed-in user off a guarded page for what is
 * really our outage.
 */
export type AuthState =
  | { status: "loading" }
  | { status: "signed-in"; me: Me }
  | { status: "anonymous" }
  | { status: "unreachable" };

/** The site is a static export, so this is the only way to learn who is signed in. */
export function useAuth(): AuthState {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();

    (async () => {
      try {
        // credentials: "include" — without it the session cookie is not sent
        // when the API is on another origin in local development.
        const res = await fetch(ME_PATH, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (res.status === 401) {
          setState({ status: "anonymous" });
          return;
        }
        if (!res.ok) {
          setState({ status: "unreachable" });
          return;
        }
        setState({ status: "signed-in", me: (await res.json()) as Me });
      } catch {
        if (!controller.signal.aborted) setState({ status: "unreachable" });
      }
    })();

    return () => controller.abort();
  }, []);

  return state;
}

/** What to call someone: their display name, else their email address. */
export function displayNameOf(me: Me): string {
  return me.user.display_name?.trim() || me.user.email;
}
