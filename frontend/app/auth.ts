"use client";

import { useEffect, useState } from "react";

import { CHOOSE_ACCOUNT_PARAM, ME_PATH, SIGN_IN_PATH, SWITCH_ACCOUNT_PATH } from "./api";

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

/**
 * Did this page load come straight from a sign-out?
 *
 * Read once per page load and latched, because reading is destructive: the
 * marker is stripped from the address bar as soon as it is seen. It describes
 * one moment, and left in the URL it would outlive it — bookmarked, shared or
 * refreshed later, it would keep forcing the account picker on someone who is
 * no longer choosing anything. replaceState rather than pushState, so the back
 * button does not land on the same URL again.
 *
 * The latch is what makes the two callers agree: the nav and the hero both ask,
 * and their effects run one after the other, so an unlatched read would have
 * the second caller see an already-cleaned URL and offer a different sign-in
 * button from the first.
 */
let signedOutMarker: boolean | null = null;

function justSignedOut(): boolean {
  if (signedOutMarker !== null) return signedOutMarker;
  const url = new URL(window.location.href);
  signedOutMarker = url.searchParams.has(CHOOSE_ACCOUNT_PARAM);
  if (signedOutMarker) {
    url.searchParams.delete(CHOOSE_ACCOUNT_PARAM);
    window.history.replaceState(null, "", url.toString());
  }
  return signedOutMarker;
}

/**
 * Where the "Sign in" button should point.
 *
 * Normally the plain flow, so a first-time or returning visitor is not made to
 * click through an account picker for no reason. But after a sign-out — which
 * the landing URL carries as a marker — it becomes the picker flow instead:
 * Microsoft would otherwise reuse the still-live browser SSO session and hand
 * back the exact account the user just left, with no way to say otherwise.
 *
 * Resolved in an effect rather than during render: this is a static export, so
 * the server-rendered HTML cannot see the query string and reading it inline
 * would be a hydration mismatch. The href is only ever corrected, never
 * removed, so the button works throughout.
 */
export function useSignInHref(): string {
  const [href, setHref] = useState(SIGN_IN_PATH);

  useEffect(() => {
    if (justSignedOut()) setHref(SWITCH_ACCOUNT_PATH);
  }, []);

  return href;
}

/** What to call someone: their display name, else their email address. */
export function displayNameOf(me: Me): string {
  return me.user.display_name?.trim() || me.user.email;
}
