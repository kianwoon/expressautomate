"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  NOTIFICATIONS_SETTINGS_PATH,
  NOTIFICATIONS_SUBSCRIPTIONS_PATH,
  TELEGRAM_LINK_PATH,
  WHATSAPP_LINKED_DESTINATION_PATH,
  notificationDestinationPath,
} from "../../api";

/**
 * Reading and changing notification preferences.
 *
 * Data lives here and rendering lives in the components, the same split
 * `glossary-data.ts` uses. The screen has one read and three writes, and every
 * write ends by re-reading rather than patching local state: the server owns
 * whether a destination is verified or disabled, and a client-side guess at
 * that would drift the first time something failed server-side.
 *
 * allow-hardcode: the labels and messages below are user-facing copy, not a
 * list anything is matched against.
 */

export type NotificationEvent = { kind: string };

export type Destination = {
  id: string;
  channel: string;
  scope: string;
  verified: boolean;
  disabled: boolean;
  event_kinds: string[];
};

export type NotificationSettings = {
  channels: Record<string, boolean>;
  // Present only once the recruiter has a CONNECTED paired WhatsApp device
  // (see `WA_SESSION_PATH`) — distinct from `channels.whatsapp`, which is the
  // unrelated Meta Cloud API channel.
  whatsapp_linked_number: string | null;
  destinations: Destination[];
  events: NotificationEvent[];
};

export type NotificationsState =
  | { status: "loading" }
  // `refreshError` is set only when a load AFTER the first success fails — the
  // Telegram panel polls this every 3s, and a transient blip there must not
  // replace the destinations and the in-progress panel with a full-screen
  // error. Exception: a 401 (expired session) is non-recoverable, so it always
  // collapses to `unreadable` to stop the polling and show the sign-in prompt.
  // The first, un-refreshed load has nothing to fall back to, so it still goes
  // to `unreadable` below.
  | { status: "ready"; settings: NotificationSettings; refreshError?: string }
  | { status: "unreadable"; message: string };

/**
 * What each event kind is called on screen.
 *
 * The API returns kinds, not labels, deliberately — a wire format should not
 * carry prose. An unknown kind falls back to its raw value rather than being
 * hidden, so a kind added server-side appears (ugly but present) instead of
 * silently vanishing from a screen the recruiter trusts to be complete.
 */
export const EVENT_LABELS: Record<string, string> = {
  "opportunity.new": "A new job order arrives",
  "opportunity.needs_review": "A job order needs review",
};

export function eventLabel(kind: string): string {
  return EVENT_LABELS[kind] ?? kind;
}

export function useNotifications() {
  const [state, setState] = useState<NotificationsState>({ status: "loading" });

  // A generation counter, bumped on every `load()` call. The Telegram panel
  // polls every 3s while a checkbox toggle also triggers its own reload after
  // a PUT; without ordering, a poll issued before the PUT can resolve after
  // it and overwrite the just-saved state with stale data. Each call captures
  // its own generation and discards its response if a newer load has since
  // started — except that a load a mutation itself awaited answers only to
  // newer MUTATION loads (`mutationGeneration`), not to polls. A poll that
  // starts mid-mutation-reload fetched after the PUT too, so letting both
  // apply is safe in either completion order; discarding the mutation's own
  // reload (the old rule) left the pre-write state on screen until the poll
  // landed.
  const generation = useRef(0);
  const mutationGeneration = useRef(0);

  const load = useCallback((signal?: AbortSignal, afterMutation = false) => {
    const thisGeneration = ++generation.current;
    if (afterMutation) mutationGeneration.current = thisGeneration;
    // Which newer starts may discard this response once it arrives: any newer
    // load for a poll, but only a newer mutation's load for a mutation.
    const superseded = () =>
      afterMutation
        ? mutationGeneration.current !== thisGeneration
        : generation.current !== thisGeneration;
    return (async () => {
      try {
        const res = await fetch(NOTIFICATIONS_SETTINGS_PATH, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal,
        });
        if (superseded()) return;
        if (!res.ok) {
          const message =
            res.status === 401
              ? "Your session has expired. Sign in again and this page will show your settings."
              : "We could not read your notification settings just now.";
          // A 401 is non-recoverable — an expired session will never succeed, and the
          // polling panel would produce a stream of failures with no way to complete.
          // All other failures (network, 5xx) keep the last-good data on screen since
          // they genuinely are transient. But 401 must collapse to the full-screen
          // unreadable state so the expired-session message is all the recruiter sees.
          setState((prev) =>
            res.status === 401
              ? { status: "unreadable", message }
              : prev.status === "ready"
                ? { ...prev, refreshError: message }
                : { status: "unreadable", message },
          );
          return;
        }
        const settings = (await res.json()) as NotificationSettings;
        if (superseded()) return;
        setState({ status: "ready", settings });
      } catch {
        if (superseded()) return;
        if (!signal?.aborted) {
          const message = "We could not reach the server.";
          setState((prev) => (prev.status === "ready" ? { ...prev, refreshError: message } : { status: "unreadable", message }));
        }
      }
    })();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  /** Replace one destination's events. `kinds` must be the FULL set. */
  const setEvents = useCallback(
    async (destinationId: string, kinds: string[]): Promise<string | null> => {
      try {
        const res = await fetch(NOTIFICATIONS_SUBSCRIPTIONS_PATH, {
          method: "PUT",
          credentials: "include",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ destination_id: destinationId, event_kinds: kinds }),
        });
        if (!res.ok) return "We could not save that just now.";
        await load(undefined, true);
        return null;
      } catch {
        return "We could not reach the server.";
      }
    },
    [load],
  );

  const unlink = useCallback(
    async (destinationId: string): Promise<string | null> => {
      try {
        const res = await fetch(notificationDestinationPath(destinationId), {
          method: "DELETE",
          credentials: "include",
          headers: { Accept: "application/json" },
        });
        // 204 on success. `res.ok` covers it; a 404 means it is already gone,
        // which is the state the caller wanted, so it is not an error.
        if (!res.ok && res.status !== 404) return "We could not unlink that just now.";
        await load(undefined, true);
        return null;
      } catch {
        return "We could not reach the server.";
      }
    },
    [load],
  );

  const requestTelegramLink = useCallback(async (): Promise<
    { url: string; expiresInMinutes: number } | { error: string }
  > => {
    try {
      const res = await fetch(TELEGRAM_LINK_PATH, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (res.status === 503) {
        return { error: "Telegram is not set up on this deployment yet." };
      }
      if (!res.ok) return { error: "We could not create a link just now." };
      const body = (await res.json()) as { url: string; expires_in_minutes: number };
      // The expiry comes from the server, never a constant here: the backend
      // reads it from NOTIFY_LINK_TOKEN_TTL_MINUTES, and a copy in this file
      // would disagree with it the first time an operator changed the setting.
      return { url: body.url, expiresInMinutes: body.expires_in_minutes };
    } catch {
      return { error: "We could not reach the server." };
    }
  }, []);

  const addWhatsAppLinkedDestination = useCallback(async (): Promise<string | null> => {
    try {
      const res = await fetch(WHATSAPP_LINKED_DESTINATION_PATH, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      // 400 is the only refusal this endpoint has, and it means exactly one
      // thing: no connected device. The backend chose 400 over 409 to match
      // the other linking routes in that file, so match it here rather than
      // letting the specific sentence fall through to the generic one.
      if (res.status === 400) {
        return "You need a connected WhatsApp device first — link one in Settings -> WhatsApp.";
      }
      if (!res.ok) return "We could not add that just now.";
      await load(undefined, true);
      return null;
    } catch {
      return "We could not reach the server.";
    }
  }, [load]);

  return {
    state,
    reload: load,
    setEvents,
    unlink,
    requestTelegramLink,
    addWhatsAppLinkedDestination,
  };
}
