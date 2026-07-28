"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { CHOOSE_ACCOUNT_PARAM, ME_PATH, SIGN_IN_PATH, SWITCH_ACCOUNT_PATH } from "./api";
import { useLive } from "./events";

export type Me = {
  user: { id: string; email: string; display_name: string | null; role: string };
  tenant: { id: string; name: string; is_personal_account: boolean };
  mailbox: {
    provider: string;
    /** The grant. Survives the mailbox going bad, so never show it alone. */
    connected: boolean;
    /** Granted, but the user has not yet said how far back to read. */
    awaiting_period: boolean;
    scopes: string[];
    /** null until a mailbox exists; "needs_reauth" is the one to surface. */
    status: "active" | "needs_reauth" | "disconnected" | null;
    /** An active mailbox with a live Graph subscription behind it. */
    ingestion_active: boolean;
    /** Counted from the stored rows. Never estimated, never rounded. */
    /**
     * `in_progress` is being fetched right now; `awaiting_extraction` is
     * stored and waiting for a stage that is not deployed. Kept apart because
     * calling both "processing" told people work was seconds away when it was
     * waiting on a release.
     */
    ingested: {
      total: number;
      in_progress: number;
      awaiting_extraction: number;
      /** Emails that finished extraction — how much mail was read. */
      emails_extracted: number;
      /**
       * Vacancies found in them — what the job-orders table lists. Different
       * from `emails_extracted`: one email can advertise six roles, and one
       * did, which is how the page showed "3 job orders found" above a table
       * of 7. Anything labelled "job orders" must use this one.
       */
      opportunities: number;
    };
    oldest_received: string | null;
    newest_received: string | null;
    last_activity: string | null;
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

/**
 * The site is a static export, so this is the only way to learn who is signed in.
 *
 * `live` is opt-in and off by default. Most callers — the nav, the footer, a
 * marketing page checking whether to offer "Sign in" — ask once, because the
 * answer does not change while the page is open. Following the stream from all
 * of them would open a long-lived connection on behalf of every component that
 * merely wanted a name. The dashboard opts in, because for that page the mailbox
 * block inside `me` is live data: the counts, the ingestion state, and the
 * "Starting up" → "Reading new mail" transition all live there.
 */
export function useAuth(live = false): AuthState {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const generation = useRef(0);
  // A poll must not overtake the first, visible load. If it did and then failed
  // quietly — the rule below, which exists to protect a signed-in user from a
  // blip — the discarded response would be the one the page is waiting on, and
  // the dashboard would sit on "Checking your session."
  // A count, released unconditionally — see the note in
  // dashboard/opportunities.ts for why ownership-by-newest-ticket was wrong.
  const loud = useRef(0);

  const load = useCallback(async (quiet: boolean): Promise<void> => {
    if (quiet && loud.current > 0) return;
    if (!quiet) loud.current += 1;

    const mine = ++generation.current;
    const superseded = () => mine !== generation.current;
    const settle = () => {
      if (!quiet) loud.current -= 1;
    };

    try {
      // credentials: "include" — without it the session cookie is not sent
      // when the API is on another origin in local development.
      const res = await fetch(ME_PATH, {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (superseded()) return;
      if (res.status === 401) {
        // Said even on a background refresh, and deliberately: the session is
        // genuinely gone, and the guarded page above this hook has to stop
        // showing one tenant's mailbox to whoever is now at the keyboard.
        setState({ status: "anonymous" });
        return;
      }
      if (!res.ok) {
        // A refresh that fails leaves a signed-in user signed in. Demoting them
        // to "unreachable" would swap the whole dashboard for an outage notice
        // over one bad response, when the next event or tab refocus will correct
        // it and the user has lost nothing in the meantime.
        if (quiet) return;
        setState({ status: "unreachable" });
        return;
      }
      const me = (await res.json()) as Me;
      if (superseded()) return;
      setState({ status: "signed-in", me });
    } catch {
      if (superseded() || quiet) return;
      setState({ status: "unreachable" });
    } finally {
      settle();
    }
  }, []);

  useEffect(() => {
    void load(false);
    return () => {
      generation.current += 1;
    };
  }, [load]);

  // Every kind changes something in here — the counts, the mailbox state, the
  // grant — so this one listens to all of them rather than picking.
  useLive(() => void load(true), live);

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
