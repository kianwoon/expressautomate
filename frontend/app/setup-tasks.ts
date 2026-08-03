// What is left of onboarding, for the nav bell (setup-bell.tsx).
//
// Two checklist items, each read from the settings screen that already owns
// that feature's true state — this file adds no new backend concept, it only
// asks two existing endpoints "is this configured yet?" and turns "no" into a
// nudge. A mailbox item is deliberately absent: the dashboard already owns
// that onboarding path, and duplicating it here would give a recruiter two
// different places claiming to track the same step.
//
// The fetching and the pure "which items are outstanding" logic are kept in
// one file, but the logic itself (`outstandingTasks`) takes plain data and
// returns plain data, so it is exercised in tests without a DOM or a mocked
// `fetch` — see setup-tasks.test.ts.

import { CLIENT_DISCOVERY_PATH, NOTIFICATIONS_SETTINGS_PATH, SETTINGS_CLIENT_DISCOVERY_PATH, SETTINGS_NOTIFICATIONS_PATH } from "./api";

export type SetupTaskId = "notifications" | "client-discovery";

export type SetupTask = {
  id: SetupTaskId;
  /** The checklist line itself. */
  label: string;
  /** One line of "why this helps", written for a Singapore recruitment-agency
   *  recruiter rather than a generic SaaS nudge. */
  subtitle: string;
  /** Where the row's link goes — the settings page that finishes the step. */
  href: string;
};

/**
 * Only the slice of `GET /api/notifications/settings` this file needs.
 *
 * The full shape (with events, whatsapp_linked_number and per-destination
 * event_kinds) lives in `settings/notifications/notifications-data.ts` as
 * `NotificationSettings` — this type is intentionally narrower rather than
 * importing that one, because that file is `"use client"` React-hook code
 * and pulling it in here would drag a hook module into a plain data module
 * for the sake of a handful of fields.
 */
type NotificationsSettingsResponse = {
  destinations: { disabled: boolean }[];
};

/** Only the slice of `GET /api/client-discovery` this file needs — see
 *  `settings/client-discovery/discovery-panel.tsx` for the full `Run` shape. */
type ClientDiscoveryResponse = {
  run: unknown | null;
};

/**
 * Item 1 is done once at least one destination — WhatsApp (either channel) or
 * Telegram, they are interchangeable for this checklist — is not disabled.
 * `notifications.py::notification_settings` (backend/app/api/notifications.py)
 * only ever creates a `notification_destinations` row for one of those three
 * channels, so "any enabled destination exists" already means "WhatsApp or
 * Telegram is set up" without this file needing to name the channels itself.
 */
function notificationsConfigured(response: NotificationsSettingsResponse): boolean {
  return response.destinations.some((destination) => !destination.disabled);
}

/**
 * Item 2 is done once a scan has ever been started — `run` moves from `null`
 * to a row the moment `POST /client-discovery/scan` is called
 * (backend/app/api/client_discovery.py::start_scan), before it even finishes.
 * That is deliberate here: the checklist's job is to get the recruiter to
 * turn the feature on, not to grade whether the first scan succeeded — a
 * failed run still means they configured it, and disappearing the nudge
 * only to have client-discovery's own screen explain a failure is the
 * clearer split of responsibility.
 */
function clientDiscoveryConfigured(response: ClientDiscoveryResponse): boolean {
  return response.run !== null;
}

/** The checklist, in the order it is shown. Copy is deliberately concrete
 *  about the Singapore-agency workflow rather than generic SaaS "finish
 *  setup" language — see the CLAUDE.md note on the vertical. */
const NOTIFICATIONS_TASK: SetupTask = {
  id: "notifications",
  label: "Get alerts on WhatsApp or Telegram",
  subtitle: "Know a new job order landed the moment it does, instead of finding it next time you open the inbox.",
  href: SETTINGS_NOTIFICATIONS_PATH,
};

const CLIENT_DISCOVERY_TASK: SetupTask = {
  id: "client-discovery",
  label: "Turn on client discovery",
  subtitle: "Let your own sent mail surface the clients already in it, instead of rebuilding that list from memory.",
  href: SETTINGS_CLIENT_DISCOVERY_PATH,
};

/**
 * The pure decision: given both settings responses, which items are still
 * outstanding, in checklist order. Unit-tested directly with hand-built
 * response fixtures — no DOM, no `fetch` — in setup-tasks.test.ts.
 */
export function outstandingTasks(
  notifications: NotificationsSettingsResponse,
  clientDiscovery: ClientDiscoveryResponse,
): SetupTask[] {
  const tasks: SetupTask[] = [];
  if (!notificationsConfigured(notifications)) tasks.push(NOTIFICATIONS_TASK);
  if (!clientDiscoveryConfigured(clientDiscovery)) tasks.push(CLIENT_DISCOVERY_TASK);
  return tasks;
}

/**
 * Fetches both settings screens and returns the outstanding items, or `null`
 * on any failure — a network error, a non-2xx response, or an aborted
 * request (the caller unmounted mid-fetch). `null` is deliberately not
 * distinguished from "everything is done": either way the bell must render
 * nothing, because an onboarding nudge is never worth turning into a visible
 * error state in the nav.
 *
 * `Promise.all` rather than sequential awaits: the two reads are independent
 * and there is no reason to make a recruiter's first paint wait twice as
 * long for a nav badge.
 */
export async function fetchOutstandingSetupTasks(signal?: AbortSignal): Promise<SetupTask[] | null> {
  try {
    const [notificationsRes, clientDiscoveryRes] = await Promise.all([
      fetch(NOTIFICATIONS_SETTINGS_PATH, {
        credentials: "include",
        headers: { Accept: "application/json" },
        signal,
      }),
      fetch(CLIENT_DISCOVERY_PATH, {
        credentials: "include",
        headers: { Accept: "application/json" },
        signal,
      }),
    ]);
    if (!notificationsRes.ok || !clientDiscoveryRes.ok) return null;
    const notifications = (await notificationsRes.json()) as NotificationsSettingsResponse;
    const clientDiscovery = (await clientDiscoveryRes.json()) as ClientDiscoveryResponse;
    return outstandingTasks(notifications, clientDiscovery);
  } catch {
    // Covers a network failure and an aborted fetch alike — the caller checks
    // `signal.aborted` itself before acting on a `null` from an unmount, so
    // this file does not need to tell the two apart.
    return null;
  }
}
