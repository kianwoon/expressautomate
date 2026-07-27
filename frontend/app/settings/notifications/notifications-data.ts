"use client";

import { useCallback, useEffect, useState } from "react";

import {
  NOTIFICATIONS_SETTINGS_PATH,
  NOTIFICATIONS_SUBSCRIPTIONS_PATH,
  TELEGRAM_LINK_PATH,
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
  destinations: Destination[];
  events: NotificationEvent[];
};

export type NotificationsState =
  | { status: "loading" }
  | { status: "ready"; settings: NotificationSettings }
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

  const load = useCallback((signal?: AbortSignal) => {
    return (async () => {
      try {
        const res = await fetch(NOTIFICATIONS_SETTINGS_PATH, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal,
        });
        if (!res.ok) {
          setState({
            status: "unreadable",
            message:
              res.status === 401
                ? "Your session has expired. Sign in again and this page will show your settings."
                : "We could not read your notification settings just now.",
          });
          return;
        }
        setState({ status: "ready", settings: (await res.json()) as NotificationSettings });
      } catch {
        if (!signal?.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
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
        await load();
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
        await load();
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

  return { state, reload: load, setEvents, unlink, requestTelegramLink };
}
