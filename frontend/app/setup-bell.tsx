"use client";

import { useEffect, useId, useRef, useState } from "react";

import { useAuth } from "./auth";
import { fetchOutstandingSetupTasks, type SetupTask } from "./setup-tasks";

/**
 * The iPhone-style setup nudge in the nav's auth corner.
 *
 * It exists for exactly one moment in a recruiter's life: the stretch between
 * signing up and finishing the two things that make the product actually
 * useful (an alert channel, client discovery). Once both are done the bell
 * has nothing left to say and is gone — there is no "all caught up" state to
 * design, because there is nothing to check back on afterwards.
 *
 * Reserving the corner rather than reflowing it: `.setup-bell-slot` always
 * takes the same width in the signed-in nav, whether or not a bell ends up
 * inside it. Without that, a recruiter who finishes both steps mid-session — or
 * whose fetch simply resolves a beat after first paint — would see Settings
 * and the account menu physically jump left the instant the bell disappears
 * or appears. The cost is a few pixels of permanently invisible gap for a
 * fully onboarded recruiter, which is cheaper than a nav that shifts under a
 * pointer mid-click.
 *
 * Fetches once, only once signed in, and never renders an error: a stalled
 * onboarding nudge is not worth a broken-looking nav icon, so any failure
 * (see `fetchOutstandingSetupTasks`) just leaves the slot empty forever.
 */
export function SetupBell() {
  const auth = useAuth();
  // `null` covers both "not answered yet" and "answered, nothing outstanding
  // or the fetch failed" — see the comment on `visible` below for how those
  // last two are told apart from "still loading".
  const [tasks, setTasks] = useState<SetupTask[] | null>(null);
  const [resolved, setResolved] = useState(false);

  useEffect(() => {
    if (auth.status !== "signed-in") return;
    const controller = new AbortController();
    fetchOutstandingSetupTasks(controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setTasks(result);
      setResolved(true);
    });
    return () => controller.abort();
    // Deliberately keyed on the status string, not the whole `auth` object —
    // `auth.me` is a fresh object on every poll from `useAuth`, and this fetch
    // must run once, not on every re-render that happens to produce a new
    // (structurally equal) `me`.
  }, [auth.status]);

  // Nothing to show before the fetch answers, and nothing to show once it
  // has answered "no tasks" or "it failed" — both look identical to a
  // recruiter, and both are the reason `fetchOutstandingSetupTasks` folds
  // failure into `null` rather than a separate error type worth surfacing.
  const outstanding = resolved && tasks !== null && tasks.length > 0 ? tasks : null;

  // The button is rendered either way and hidden by the slot when there is
  // nothing outstanding, rather than dropped from the tree. Dropping it made
  // the reserved space a width written out in globals.css that had to be kept
  // equal to `.btn-icon`'s real size by hand — and the moment those disagreed,
  // the corner moved. Hidden, the space reserved *is* the button.
  return (
    <div className="setup-bell-slot" data-visible={outstanding ? "yes" : "no"}>
      <BellButton tasks={outstanding ?? []} />
    </div>
  );
}

/** Inline, `currentColor`, `aria-hidden` — a shape, not content. Keeping it
 *  inline avoids a dependency for one icon; `frontend`'s only runtime deps
 *  are `next`, `react` and `qrcode`. */
function BellIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
      <path
        d="M12 3c-3.31 0-6 2.69-6 6v3.09c0 .55-.16 1.09-.46 1.55L4.2 15.6a1 1 0 0 0 .84 1.55h13.92a1 1 0 0 0 .84-1.55l-1.34-1.96A2.9 2.9 0 0 1 18 12.09V9c0-3.31-2.69-6-6-6Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M9.5 19a2.5 2.5 0 0 0 5 0" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

/**
 * The trigger and its dropdown, once there is something to nudge about.
 *
 * Click-only, unlike `AccountMenu`'s hover-to-open in site-nav.tsx: that menu
 * opens on hover because it is where the pointer already rests on the way to
 * "sign out" or "switch account" — an incidental hover there costs nothing.
 * A bell earns no such assumption; a cursor merely passing over it on the way
 * to Settings should not pop a panel open under it. So this opens on a real
 * click only — not on focus either, unlike `AccountMenu`. A native
 * `<button>` already activates on Enter/Space when focused, so Tab-then-Enter
 * reaches the same open state without a separate focus-opens rule earning
 * its own bug: `AccountMenu`'s Escape handler calls `.focus()` on the
 * trigger it already holds focus, a no-op there, but a bell most browsers
 * (Safari among them) do not focus on click, so an Escape-triggered
 * `.focus()` here would be a genuine new focus event and — with a focus-opens
 * rule — reopen the very panel Escape just closed. Leaving focus out of what
 * opens the panel sidesteps that rather than patching around it.
 *
 * Blur-outside stays: `onBlur`'s `relatedTarget` check closes it for a click
 * that lands anywhere else in the document (a click on a non-focusable
 * element moves focus to `<body>`, which the wrapper does not contain), so
 * there is no separate document-level listener to keep in sync with the
 * panel's lifetime.
 */
function BellButton({ tasks }: { tasks: SetupTask[] }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);

  const count = tasks.length;
  const countLabel = `${count} setup step${count === 1 ? "" : "s"} left`;

  return (
    <div
      className="nav-menu"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(e) => {
        if (e.key !== "Escape" || !open) return;
        setOpen(false);
        triggerRef.current?.focus();
      }}
    >
      <button
        ref={triggerRef}
        className="btn btn-secondary btn-icon nav-bell-trigger"
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={countLabel}
        onClick={() => setOpen((wasOpen) => !wasOpen)}
      >
        <BellIcon />
        {/* The visible digit is decorative next to the button's own
            `aria-label` above — a screen reader announces "2 setup steps
            left", not "2" followed by an unlabelled bell shape. */}
        <span className="nav-bell-badge" aria-hidden="true">
          {count}
        </span>
      </button>
      {open && (
        <div className="nav-menu-panel card nav-bell-panel" id={panelId}>
          <p className="nav-bell-heading">Finish setting up</p>
          {tasks.map((task) => (
            <a key={task.id} className="nav-bell-item" href={task.href}>
              <span className="nav-bell-item-label">{task.label}</span>
              <span className="nav-bell-item-subtitle">{task.subtitle}</span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
