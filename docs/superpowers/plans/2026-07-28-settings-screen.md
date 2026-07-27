# Settings Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the notification system a screen a recruiter can actually use, and restructure Settings so it can hold one.

**Architecture:** `/settings` becomes three routes under a shared shell that owns the nav and the auth guard. The new notifications route reads one endpoint, renders one card per linked destination, and drives Telegram linking through a panel that polls until the destination appears.

**Tech Stack:** Next.js 15 (App Router, static export), React 19, TypeScript, hand-rolled CSS in one global stylesheet. One new dependency: `qrcode`.

**Spec:** [docs/superpowers/specs/2026-07-28-settings-screen-design.md](../specs/2026-07-28-settings-screen-design.md)

## Global Constraints

- **There is no test framework.** No jest, vitest or playwright. TDD as written in the sub-skill does not apply. Every task instead ends with three real verifications: `npm run build` succeeds (this is a static export — a broken route fails the build), `npx next lint` is clean, and a named manual observation in the browser. Do not invent test files; do not add a framework.
- **Every component that fetches does so itself**, with `credentials: "include"` and `headers: { Accept: "application/json" }`. There is no shared fetch wrapper and this plan does not add one — see the spec's reasoning. Copy the shape from `frontend/app/settings/lookback.tsx:46-50`.
- **State is a discriminated union**, never a pile of booleans. See `lookback.tsx:26-33` for the house form: `{ status: "loading" } | { status: "ready"; … } | { status: "unreadable"; message: string }`.
- **Load errors and mutation errors are separate state.** `lookback.tsx:38-41` records why: one means we could not read the setting, the other means we read it and could not change it. They need different words, and the second must not wipe the page.
- **CSS classes are feature-prefixed** in `frontend/app/globals.css` — `gl-*` for glossary, `jo-*` for job orders. Notifications uses **`nt-*`**. No CSS modules, no component library, no inline style objects beyond the one-off spacing the existing pages already use.
- **User-facing copy carries `allow-hardcode:`** in the file's docstring comment, stating it is copy rendered to the page and not a list anything is matched against. Every existing settings file does this.
- **API paths are constants in `frontend/app/api.ts`**, with a doc comment explaining what the endpoint is for. API routes are prefixed `${API_BASE}`; site routes are not.
- **`"use client"` on every component that uses hooks.** The whole signed-in surface is client-rendered because the site is a static export.
- Comments explain reasoning and trade-offs, never restate the code. Match the density of the surrounding files.
- Commands run from `frontend/`.

---

## File Structure

**Create:**

| Path | Responsibility |
|---|---|
| `app/settings/settings-shell.tsx` | Nav, sub-nav, auth guard, footer — the frame every settings route renders inside |
| `app/settings/glossary/page.tsx` | Route wrapper rendering the existing `Glossary` |
| `app/settings/notifications/page.tsx` | Compose the notifications screen from its parts |
| `app/settings/notifications/notifications-data.ts` | `useNotifications()` — fetch, mutations, state |
| `app/settings/notifications/destination-card.tsx` | One destination: state, event checkboxes, unlink |
| `app/settings/notifications/telegram-link-panel.tsx` | Link, QR, polling |

**Modify:** `app/settings/page.tsx` (adopt shell, drop guard), `app/api.ts` (new paths), `app/globals.css` (`nt-*`), `package.json` (`qrcode`).

**Leave alone:** `app/settings/lookback.tsx`, `app/settings/glossary.tsx`, `glossary-form.tsx`, `glossary-data.ts` — they move routes but their internals do not change.

---

## Task 1: Settings shell and sub-navigation

**Files:**
- Create: `app/settings/settings-shell.tsx`
- Create: `app/settings/glossary/page.tsx`
- Modify: `app/settings/page.tsx`
- Modify: `app/api.ts`
- Modify: `app/globals.css`

**Interfaces:**
- Consumes: `useAuth()` from `app/auth.ts`, returning `{status: "loading"} | {status: "signed-in", me: Me} | {status: "anonymous"} | {status: "unreachable"}`
- Produces: `<SettingsShell eyebrow={string} heading={string} active="inbox"|"glossary"|"notifications">{children}</SettingsShell>`; constants `SETTINGS_GLOSSARY_PATH`, `SETTINGS_NOTIFICATIONS_PATH`

- [ ] **Step 1: Add the two site route constants**

In `app/api.ts`, immediately after the existing `SETTINGS_PATH` export:

```ts
/**
 * The settings sub-routes. Site routes, so no API_BASE prefix.
 *
 * `/settings` itself keeps the inbox setting rather than redirecting to
 * `/settings/inbox`: this is a static export, so a redirect would have to
 * render something first and flash, and existing links to `/settings` still
 * land where they always did.
 */
export const SETTINGS_GLOSSARY_PATH = "/settings/glossary";
export const SETTINGS_NOTIFICATIONS_PATH = "/settings/notifications";
```

- [ ] **Step 2: Write the shell**

Create `app/settings/settings-shell.tsx`:

```tsx
"use client";

import { useEffect } from "react";

import { LANDING_PATH, SETTINGS_GLOSSARY_PATH, SETTINGS_NOTIFICATIONS_PATH, SETTINGS_PATH } from "../api";
import { useAuth } from "../auth";
import { SiteFooter } from "../site-footer";
import { SiteNav } from "../site-nav";

/**
 * The frame every settings route renders inside.
 *
 * The auth guard lived in `settings/page.tsx` and is duplicated again in
 * `dashboard/page.tsx`. Splitting settings into three routes would have made
 * four copies of a piece of reasoning that is easy to get subtly wrong — the
 * important part being that `unreachable` is NOT `anonymous`, so an outage of
 * ours never pushes a signed-in user off a guarded page. One copy here now
 * covers all three settings routes. The dashboard keeps its own, since it is
 * not a settings route.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
 */

type Tab = "inbox" | "glossary" | "notifications";

const TABS: { key: Tab; label: string; href: string }[] = [
  { key: "inbox", label: "Inbox", href: SETTINGS_PATH },
  { key: "glossary", label: "Shorthand", href: SETTINGS_GLOSSARY_PATH },
  { key: "notifications", label: "Notifications", href: SETTINGS_NOTIFICATIONS_PATH },
];

export function SettingsShell({
  heading,
  active,
  children,
}: {
  heading: string;
  active: Tab;
  children: React.ReactNode;
}) {
  const auth = useAuth();

  // Only a real 401 sends you away, and it goes to the landing page rather
  // than straight into a provider redirect — the choice of provider is the
  // user's.
  useEffect(() => {
    if (auth.status === "anonymous") window.location.replace(LANDING_PATH);
  }, [auth.status]);

  return (
    <>
      <SiteNav />
      <main>
        <section className="hero" style={{ paddingBottom: 48 }}>
          <div className="wrap" aria-live="polite">
            <span className="eyebrow">Settings</span>
            <h1 style={{ marginTop: 14, fontSize: "clamp(1.75rem, 3.4vw, 2.5rem)" }}>{heading}</h1>
            <nav className="nt-tabs" aria-label="Settings sections">
              {TABS.map((t) => (
                <a
                  key={t.key}
                  href={t.href}
                  className={t.key === active ? "nt-tab nt-tab-on" : "nt-tab"}
                  aria-current={t.key === active ? "page" : undefined}
                >
                  {t.label}
                </a>
              ))}
            </nav>
            {auth.status === "signed-in" ? (
              children
            ) : auth.status === "unreachable" ? (
              <p className="lede" style={{ marginTop: 18 }}>
                We could not reach the server. This is not a sign-in problem — your session is
                untouched. Reload the page in a moment.
              </p>
            ) : (
              /* Covers both "loading" and the instant before the redirect
                 fires on "anonymous". Nothing about the account is asserted
                 before the check resolves — otherwise a signed-out visitor
                 sees a flash of signed-in chrome on the way out. */
              <p className="lede" style={{ marginTop: 18 }}>
                Checking your session.
              </p>
            )}
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}
```

- [ ] **Step 3: Add the tab styles**

Append to `app/globals.css`:

```css
/* Settings sub-navigation. `nt-` is the notifications/settings prefix, in the
   same style as `gl-` (glossary) and `jo-` (job orders). */
.nt-tabs {
  display: flex;
  gap: 22px;
  margin-top: 26px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
}
.nt-tab {
  padding: 0 2px 10px;
  font-size: 0.95rem;
  text-decoration: none;
  color: inherit;
  opacity: 0.62;
  border-bottom: 2px solid transparent;
}
.nt-tab:hover {
  opacity: 0.9;
}
.nt-tab-on {
  opacity: 1;
  border-bottom-color: currentColor;
}
```

- [ ] **Step 4: Rewrite the settings index to use the shell**

Replace the whole of `app/settings/page.tsx`:

```tsx
"use client";

import { SettingsShell } from "./settings-shell";
import { LookbackSetting } from "./lookback";

/**
 * How far back we read the inbox.
 *
 * This stays at `/settings` rather than moving to `/settings/inbox`: existing
 * links keep working, and a static export cannot redirect without rendering
 * something first. The sub-nav therefore labels it "Inbox" while the URL says
 * `/settings` — a deliberate asymmetry, taken over shipping a redirect that
 * exists only for symmetry.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export default function Settings() {
  return (
    <SettingsShell heading="Settings." active="inbox">
      <h2 className="eyebrow" style={{ marginTop: 22 }}>
        How far back we read your inbox
      </h2>
      <LookbackSetting />
    </SettingsShell>
  );
}
```

- [ ] **Step 5: Give the glossary its own route**

Create `app/settings/glossary/page.tsx`:

```tsx
"use client";

import { SettingsShell } from "../settings-shell";
import { Glossary } from "../glossary";

/**
 * The shorthand glossary, on its own route.
 *
 * `Glossary` takes `enabled` and does not fetch until it is true, so that an
 * anonymous visitor never triggers a 401 on the way to the landing page. The
 * shell only renders children once the session is confirmed, which is exactly
 * that condition — hence `enabled` is unconditionally true here.
 */

export default function GlossarySettings() {
  return (
    <SettingsShell heading="Shorthand." active="glossary">
      <Glossary enabled />
    </SettingsShell>
  );
}
```

> **Note:** `Glossary` renders its own `<section className="alt">` wrapper with its own heading. Inside the shell that nests a section within a section. Check it in the browser at Step 7; if the spacing is wrong, adjust `Glossary`'s outer element rather than the shell — the shell's job is the frame, and every route must get the same one.

- [ ] **Step 6: Build and lint**

```bash
npm run build
```

Expected: `✓ Generating static pages`, with `/settings` and `/settings/glossary` both listed in the route output. A route that fails to render fails the build — that is the strongest automatic check this codebase has.

```bash
npx next lint
```

Expected: no errors.

- [ ] **Step 7: Verify in the browser**

```bash
npm run dev
```

Visit `http://localhost:3000/settings` signed in. Observe, and do not proceed until all four hold:

1. The sub-nav shows Inbox / Shorthand / Notifications, with **Inbox** underlined.
2. The lookback control renders and its options load.
3. `/settings/glossary` shows the glossary with **Shorthand** underlined.
4. Signed out, `/settings` redirects to `/` with no flash of settings content.

The Notifications link 404s at this point. That is expected — Task 4 creates it.

- [ ] **Step 8: Commit**

```bash
git add app/api.ts app/globals.css app/settings/settings-shell.tsx app/settings/page.tsx app/settings/glossary/page.tsx
git commit -m "Give settings a frame it can grow inside"
```

---

## Task 2: Notifications data hook

**Files:**
- Create: `app/settings/notifications/notifications-data.ts`
- Modify: `app/api.ts`

**Interfaces:**
- Consumes: `SETTINGS_NOTIFICATIONS_PATH` (Task 1)
- Produces:
  - `type NotificationEvent = { kind: string }`
  - `type Destination = { id: string; channel: string; scope: string; verified: boolean; disabled: boolean; event_kinds: string[] }`
  - `type NotificationSettings = { channels: Record<string, boolean>; destinations: Destination[]; events: NotificationEvent[] }`
  - `type NotificationsState = { status: "loading" } | { status: "ready"; settings: NotificationSettings } | { status: "unreadable"; message: string }`
  - `useNotifications(): { state; reload; setEvents; unlink; requestTelegramLink }` with the exact signatures given in Step 3
  - `EVENT_LABELS: Record<string, string>`

- [ ] **Step 1: Add the API paths**

Append to `app/api.ts`:

```ts
/**
 * Everything the notifications screen needs in one read: which channels are
 * configured, which destinations exist with the events each is subscribed to,
 * and the catalogue of events. One endpoint so the screen cannot show a
 * destination and its subscriptions in disagreement.
 */
export const NOTIFICATIONS_SETTINGS_PATH = `${API_BASE}/api/notifications/settings`;

/**
 * Replaces one destination's subscriptions with exactly the set sent.
 *
 * Replace, not merge — the screen is the source of truth for which boxes are
 * ticked, and a merge would make unticking impossible. Callers must always
 * send the full set.
 */
export const NOTIFICATIONS_SUBSCRIPTIONS_PATH = `${API_BASE}/api/notifications/subscriptions`;

/** Mints a single-use `t.me` deep link. The response carries its own expiry. */
export const TELEGRAM_LINK_PATH = `${API_BASE}/api/notifications/destinations/telegram/link`;

/** One destination, for DELETE. Encoded, so an id that is not the uuid we
 *  expect cannot walk out of its own path segment. */
export function notificationDestinationPath(id: string): string {
  return `${API_BASE}/api/notifications/destinations/${encodeURIComponent(id)}`;
}
```

- [ ] **Step 2: Write the hook**

Create `app/settings/notifications/notifications-data.ts`:

```ts
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
```

- [ ] **Step 3: Build and lint**

```bash
npm run build && npx next lint
```

Expected: build succeeds, lint clean. Nothing imports the hook yet, so this only proves it compiles — which is the point of doing it as its own task.

- [ ] **Step 4: Commit**

```bash
git add app/api.ts app/settings/notifications/notifications-data.ts
git commit -m "Read and change notification preferences"
```

---

## Task 3: Destination card

**Files:**
- Create: `app/settings/notifications/destination-card.tsx`
- Modify: `app/globals.css`

**Interfaces:**
- Consumes: `Destination`, `NotificationEvent`, `eventLabel` (Task 2)
- Produces: `<DestinationCard destination={Destination} events={NotificationEvent[]} onSetEvents={(kinds: string[]) => Promise<string | null>} onUnlink={() => Promise<string | null>} />`

- [ ] **Step 1: Write the component**

Create `app/settings/notifications/destination-card.tsx`:

```tsx
"use client";

import { useState } from "react";

import { eventLabel, type Destination, type NotificationEvent } from "./notifications-data";

/**
 * One linked destination, and what it is subscribed to.
 *
 * A card rather than a row in a matrix because a destination carries state — 
 * verified, disabled, which account it belongs to — that a checkbox grid has
 * nowhere to put. With two event kinds a grid would also be mostly empty
 * chrome.
 *
 * Unlinking asks first. It is reversible, but silently dropping a destination
 * someone relies on is worse than one extra click.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

const CHANNEL_LABELS: Record<string, string> = { telegram: "Telegram", whatsapp: "WhatsApp" };

export function DestinationCard({
  destination,
  events,
  onSetEvents,
  onUnlink,
}: {
  destination: Destination;
  events: NotificationEvent[];
  onSetEvents: (kinds: string[]) => Promise<string | null>;
  onUnlink: () => Promise<string | null>;
}) {
  // Separate from any load error: this means we showed the setting and could
  // not change it, which needs different words and must not blank the card.
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  async function toggle(kind: string, on: boolean) {
    if (busy) return;
    setBusy(true);
    setError(null);
    // The endpoint REPLACES this destination's subscriptions, so the full set
    // goes every time. Sending only the changed kind would silently untick
    // everything else.
    const next = on
      ? [...destination.event_kinds, kind]
      : destination.event_kinds.filter((k) => k !== kind);
    setError(await onSetEvents(next));
    setBusy(false);
  }

  async function confirmUnlink() {
    setBusy(true);
    setError(null);
    setError(await onUnlink());
    setBusy(false);
    setConfirming(false);
  }

  return (
    <div className="nt-card">
      <div className="nt-card-head">
        <span className="nt-card-title">
          {CHANNEL_LABELS[destination.channel] ?? destination.channel}
        </span>
        {destination.disabled ? (
          <span className="nt-badge nt-badge-off">Disabled</span>
        ) : destination.verified ? (
          <span className="nt-badge">Verified</span>
        ) : (
          <span className="nt-badge nt-badge-off">Not verified</span>
        )}
      </div>

      {destination.disabled ? (
        <p className="nt-note">
          We stopped sending to this destination after repeated failures — most often the bot was
          blocked. Unlink it and link again to start over.
        </p>
      ) : null}

      <fieldset className="nt-events" disabled={busy}>
        <legend className="nt-events-legend">Tell me when</legend>
        {events.map((e) => (
          <label className="nt-event" key={e.kind}>
            <input
              type="checkbox"
              checked={destination.event_kinds.includes(e.kind)}
              onChange={(ev) => void toggle(e.kind, ev.target.checked)}
            />
            <span>{eventLabel(e.kind)}</span>
          </label>
        ))}
      </fieldset>

      {destination.event_kinds.length === 0 ? (
        <p className="nt-note">
          Nothing is ticked, so this destination will not receive anything.
        </p>
      ) : null}

      {error ? <p className="nt-error">{error}</p> : null}

      <div className="nt-card-foot">
        {confirming ? (
          <>
            <span className="nt-note">Unlink this destination?</span>
            <button className="btn btn-secondary" disabled={busy} onClick={() => void confirmUnlink()}>
              Yes, unlink
            </button>
            <button className="btn btn-secondary" disabled={busy} onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </>
        ) : (
          <button className="btn btn-secondary" disabled={busy} onClick={() => setConfirming(true)}>
            Unlink
          </button>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add the card styles**

Append to `app/globals.css`:

```css
/* Notification destination cards. */
.nt-card {
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 12px;
  padding: 20px 22px;
  margin-top: 18px;
}
.nt-card-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.nt-card-title {
  font-weight: 600;
}
.nt-badge {
  font-size: 0.78rem;
  padding: 2px 9px;
  border-radius: 999px;
  border: 1px solid currentColor;
  opacity: 0.85;
}
.nt-badge-off {
  opacity: 0.55;
}
.nt-events {
  border: 0;
  padding: 0;
  margin: 16px 0 0;
}
.nt-events-legend {
  font-size: 0.82rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  opacity: 0.7;
  padding: 0;
}
.nt-event {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}
.nt-note {
  margin-top: 12px;
  font-size: 0.92rem;
  opacity: 0.75;
  max-width: 62ch;
}
.nt-error {
  margin-top: 12px;
  font-size: 0.92rem;
  color: #ff9b9b;
}
.nt-card-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 18px;
}
```

- [ ] **Step 3: Build and lint**

```bash
npm run build && npx next lint
```

Expected: both clean.

- [ ] **Step 4: Commit**

```bash
git add app/settings/notifications/destination-card.tsx app/globals.css
git commit -m "Show what a linked destination will actually send"
```

---

## Task 4: Notifications page

**Files:**
- Create: `app/settings/notifications/page.tsx`
- Modify: `app/globals.css`

**Interfaces:**
- Consumes: `useNotifications` (Task 2), `DestinationCard` (Task 3), `SettingsShell` (Task 1)
- Produces: the `/settings/notifications` route

- [ ] **Step 1: Write the page**

Create `app/settings/notifications/page.tsx`:

```tsx
"use client";

import { SettingsShell } from "../settings-shell";
import { DestinationCard } from "./destination-card";
import { useNotifications } from "./notifications-data";

/**
 * Where job orders get sent.
 *
 * WhatsApp is rendered whether or not it is configured, greyed with a reason
 * when it is not: the API reports `channels.whatsapp`, and hiding the block
 * would leave nothing on screen to say the channel is coming. When the flag
 * flips true the block becomes live with no change here.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export default function NotificationsSettings() {
  const { state, setEvents, unlink } = useNotifications();

  return (
    <SettingsShell heading="Notifications." active="notifications">
      <p className="lede" style={{ marginTop: 18, maxWidth: "62ch" }}>
        Job orders arrive in the evening, and nobody opens a dashboard in the evening. Link a
        destination and we will tell you as they land.
      </p>

      {state.status === "loading" ? (
        <p className="nt-note">Reading your settings.</p>
      ) : state.status === "unreadable" ? (
        <p className="nt-error">{state.message}</p>
      ) : (
        <>
          {state.settings.destinations.length === 0 ? (
            <p className="nt-note">
              Nothing is linked yet, so nothing is being sent.
            </p>
          ) : (
            state.settings.destinations.map((d) => (
              <DestinationCard
                key={d.id}
                destination={d}
                events={state.settings.events}
                onSetEvents={(kinds) => setEvents(d.id, kinds)}
                onUnlink={() => unlink(d.id)}
              />
            ))
          )}

          <h2 className="eyebrow" style={{ marginTop: 34 }}>
            Add a destination
          </h2>

          <div className="nt-card nt-card-muted">
            <div className="nt-card-head">
              <span className="nt-card-title">WhatsApp</span>
              <span className="nt-badge nt-badge-off">
                {state.settings.channels.whatsapp ? "Available" : "Not yet available"}
              </span>
            </div>
            <p className="nt-note">
              WhatsApp needs a verified business account, which is still being approved. Telegram
              works today.
            </p>
          </div>
        </>
      )}
    </SettingsShell>
  );
}
```

> **Note:** the Telegram half of "Add a destination" arrives in Task 5. This task deliberately ships the page with only the WhatsApp block, so the route, the data hook and the cards can be seen working before the panel's timers are added on top.

- [ ] **Step 2: Add the muted-card style**

Append to `app/globals.css`:

```css
/* A destination that cannot be linked yet — present so the channel is visibly
   planned rather than absent, but plainly not actionable. */
.nt-card-muted {
  opacity: 0.6;
}
```

- [ ] **Step 3: Build and lint**

```bash
npm run build && npx next lint
```

Expected: build lists `/settings/notifications` in its route output.

- [ ] **Step 4: Verify against the live API**

```bash
NEXT_PUBLIC_API_BASE=https://expressautomate.app npm run dev
```

Sign in, then visit `http://localhost:3000/settings/notifications`. Observe:

1. Your existing Telegram destination renders as a card marked **Verified**.
2. Both events are ticked (they were subscribed via the API earlier).
3. Unticking one shows no error, and reloading the page keeps it unticked — proving the PUT sent the full set rather than clearing everything.
4. Re-tick it.
5. The WhatsApp block is greyed and says "Not yet available".

Step 3 is the one that matters. If both boxes end up empty after unticking one, the full-set contract is broken.

- [ ] **Step 5: Commit**

```bash
git add app/settings/notifications/page.tsx app/globals.css
git commit -m "Give the notification settings somewhere to live"
```

---

## Task 5: Telegram link panel

**Files:**
- Create: `app/settings/notifications/telegram-link-panel.tsx`
- Modify: `app/settings/notifications/page.tsx`
- Modify: `app/globals.css`
- Modify: `package.json`

**Interfaces:**
- Consumes: `requestTelegramLink`, `reload` from `useNotifications` (Task 2)
- Produces: `<TelegramLinkPanel available={boolean} onRequestLink={() => Promise<{url, expiresInMinutes} | {error}>} onPoll={() => void} linkedCount={number} />`

> **Two traps in this task, both fixed in the code below — do not "simplify" them away:**
>
> 1. `onPoll` is called on every tick to re-read the settings; it is **not** a "you have been linked" callback. Linking is detected by `linkedCount` rising, because the destination id is created server-side by the webhook and this page never learns it any other way.
> 2. The callback is held in a **ref**, not listed in the interval effect's dependencies. The parent passes an inline arrow whose identity changes every render, so a dependency on it would clear and re-create the interval on every render — the timer would keep resetting and could never reliably reach three seconds.

- [ ] **Step 1: Add the QR dependency**

```bash
npm install qrcode
npm install --save-dev @types/qrcode
```

This is the first runtime dependency in this frontend beyond React and Next. That is a deliberate decision recorded in the spec: QR encoding is fiddly and not worth hand-rolling.

- [ ] **Step 2: Write the panel**

Create `app/settings/notifications/telegram-link-panel.tsx`:

```tsx
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";

/**
 * Linking Telegram.
 *
 * The recruiter is on a laptop and Telegram is on their phone, so the QR is
 * the common path and the link is the fallback for whoever runs Telegram
 * Desktop.
 *
 * Nothing tells this page when Start is pressed — the update goes to our
 * webhook, not to the browser — so the panel polls while it is open and stops
 * the moment a destination appears. It polls ONLY while open: a settings page
 * left in a background tab issues no requests.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

/* Fast enough that pressing Start feels instant, slow enough that a
   fifteen-minute window is a few hundred requests rather than thousands. */
const POLL_MS = 3000;

type Panel =
  | { status: "closed" }
  | { status: "opening" }
  | { status: "open"; url: string; expiresAt: number }
  | { status: "expired" }
  | { status: "failed"; message: string };

export function TelegramLinkPanel({
  available,
  onRequestLink,
  onPoll,
  linkedCount,
}: {
  available: boolean;
  onRequestLink: () => Promise<{ url: string; expiresInMinutes: number } | { error: string }>;
  onPoll: () => void;
  linkedCount: number;
}) {
  const [panel, setPanel] = useState<Panel>({ status: "closed" });
  const [qr, setQr] = useState<string | null>(null);
  // The count at the moment the panel opened. Polling watches for it to rise
  // rather than for a specific id, because the id is created server-side by
  // the webhook and this page never learns it any other way.
  const baseline = useRef(linkedCount);
  // The poll callback lives in a ref, not in the effect's dependencies. The
  // parent passes an inline arrow, so its identity changes on every render; a
  // dependency on it would clear and re-create the interval each time and the
  // three-second timer would keep resetting before it ever fired.
  const poll = useRef(onPoll);
  useEffect(() => {
    poll.current = onPoll;
  }, [onPoll]);

  const open = useCallback(async () => {
    setPanel({ status: "opening" });
    setQr(null);
    baseline.current = linkedCount;
    const result = await onRequestLink();
    if ("error" in result) {
      setPanel({ status: "failed", message: result.error });
      return;
    }
    setPanel({
      status: "open",
      url: result.url,
      expiresAt: Date.now() + result.expiresInMinutes * 60_000,
    });
    try {
      setQr(await QRCode.toDataURL(result.url, { width: 220, margin: 1 }));
    } catch {
      // A missing QR is a degraded panel, not a broken one — the link below it
      // still works, so this failure is deliberately silent on screen.
      setQr(null);
    }
  }, [linkedCount, onRequestLink]);

  // A new destination appeared while we were waiting: that is the link
  // completing. Closing here is what makes the flow feel finished.
  useEffect(() => {
    if (panel.status === "open" && linkedCount > baseline.current) {
      setPanel({ status: "closed" });
      setQr(null);
    }
  }, [linkedCount, panel]);

  // Depends on the panel's status and expiry only — see the ref above for why
  // the callback is deliberately absent from this list.
  const openUntil = panel.status === "open" ? panel.expiresAt : null;
  useEffect(() => {
    if (openUntil === null) return;
    const timer = setInterval(() => {
      if (Date.now() >= openUntil) {
        // Leaving a dead QR on screen invites someone to scan it and get
        // nothing, with no explanation.
        setPanel({ status: "expired" });
        setQr(null);
        return;
      }
      poll.current();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [openUntil]);

  if (!available) {
    return (
      <div className="nt-card nt-card-muted">
        <div className="nt-card-head">
          <span className="nt-card-title">Telegram</span>
          <span className="nt-badge nt-badge-off">Not configured</span>
        </div>
        <p className="nt-note">
          Telegram is not set up on this deployment yet.
        </p>
      </div>
    );
  }

  return (
    <div className="nt-card">
      <div className="nt-card-head">
        <span className="nt-card-title">Telegram</span>
      </div>

      {panel.status === "closed" ? (
        <>
          <p className="nt-note">
            Open the link on the phone you want messages on, and press Start.
          </p>
          <div className="nt-card-foot">
            <button className="btn btn-primary" onClick={() => void open()}>
              Link Telegram
            </button>
          </div>
        </>
      ) : panel.status === "opening" ? (
        <p className="nt-note">Creating a link.</p>
      ) : panel.status === "failed" ? (
        <>
          <p className="nt-error">{panel.message}</p>
          <div className="nt-card-foot">
            <button className="btn btn-secondary" onClick={() => void open()}>
              Try again
            </button>
          </div>
        </>
      ) : panel.status === "expired" ? (
        <>
          <p className="nt-note">That link expired. Create a new one when you are ready.</p>
          <div className="nt-card-foot">
            <button className="btn btn-primary" onClick={() => void open()}>
              New link
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="nt-note">
            Scan this with your phone, or open the link if Telegram is on this machine. Then press
            Start. This page will notice by itself.
          </p>
          {qr ? <img className="nt-qr" src={qr} alt="" width={220} height={220} /> : null}
          <p className="nt-note">
            <a href={panel.url} target="_blank" rel="noreferrer noopener">
              {panel.url}
            </a>
          </p>
          <div className="nt-card-foot">
            <button
              className="btn btn-secondary"
              onClick={() => {
                setPanel({ status: "closed" });
                setQr(null);
              }}
            >
              Cancel
            </button>
          </div>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Add the QR style**

Append to `app/globals.css`:

```css
/* White plate behind the QR: the site is dark, and a dark-on-dark QR does not
   scan. */
.nt-qr {
  margin-top: 16px;
  border-radius: 8px;
  background: #fff;
  padding: 10px;
  display: block;
}
```

- [ ] **Step 4: Mount the panel**

In `app/settings/notifications/page.tsx`, change the hook destructuring to pull `reload`:

```tsx
  const { state, setEvents, unlink, requestTelegramLink, reload } = useNotifications();
```

Add the import:

```tsx
import { TelegramLinkPanel } from "./telegram-link-panel";
```

Then insert the panel immediately after the `<h2 className="eyebrow">Add a destination</h2>` line and before the WhatsApp `<div className="nt-card nt-card-muted">`:

```tsx
          <TelegramLinkPanel
            available={state.settings.channels.telegram}
            onRequestLink={requestTelegramLink}
            onPoll={() => void reload()}
            linkedCount={state.settings.destinations.length}
          />
```

- [ ] **Step 5: Build and lint**

```bash
npm run build && npx next lint
```

Expected: both clean. If the build complains that `qrcode` cannot be resolved, the install in Step 1 did not run.

- [ ] **Step 6: Verify the whole flow against the live API**

```bash
NEXT_PUBLIC_API_BASE=https://expressautomate.app npm run dev
```

Sign in, go to `/settings/notifications`, and first **unlink your existing Telegram destination** so there is something to link. Then:

1. Press **Link Telegram**. A QR appears with the `t.me/ExpressAutomate_bot?start=…` link below it.
2. Scan it with your phone and press Start in Telegram.
3. Within about three seconds and with no interaction, the panel closes by itself and a new **Verified** card appears.
4. Tick both events on the new card.
5. Open the browser's network tab, press **Link Telegram** again, and confirm requests to `/api/notifications/settings` appear roughly every 3 seconds. Press **Cancel** and confirm they stop.

Step 3 is the whole point of the task, and step 5 is what proves polling does not leak.

- [ ] **Step 7: Commit**

```bash
git add package.json package-lock.json app/settings/notifications/telegram-link-panel.tsx app/settings/notifications/page.tsx app/globals.css
git commit -m "Let a recruiter link Telegram without a terminal"
```

---

## Deployment

`frontend/**` deploys through the same workflow as the backend. Pushing to `main` builds the static export into the image and rolls out all three services.

Nothing here needs a new environment variable: `channels.telegram` and `channels.whatsapp` come from the API, which reads the settings already installed on Koyeb.

The one thing to watch after deploy is the QR. It is generated in the browser from a data URL, so a Content-Security-Policy that forbids `img-src data:` would render it blank while leaving the link working. There is no CSP on this site today; if one is ever added, this is the thing it will break first.
