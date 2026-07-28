"use client";

import { useLiveStatus } from "../events";

/**
 * Whether this page is still being told things.
 *
 * A dashboard that updates itself has a failure mode a static one does not: it
 * looks exactly the same when it has stopped. Numbers that are twenty minutes
 * stale are indistinguishable from numbers that are current, and a recruiter
 * has no way to tell which they are reading — so the page has to say. That is
 * the whole job of this: not decoration, but the difference between "nothing
 * has come in" and "nothing is getting through".
 *
 * Never colour alone. Each state says what it is in words, because a coloured
 * dot is not a message to somebody who cannot see the colour — and this
 * particular dot is the one carrying the caveat on everything else on screen.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
 */
export function LiveLight() {
  const status = useLiveStatus();

  const label =
    status === "live" ? "Live" : status === "waiting" ? "Connecting" : "Not updating";

  return (
    <span className="jo-live" data-status={status} role="status">
      <span className="jo-live-dot" aria-hidden="true" />
      {/* The visible word is short enough to sit in a header; the longer
          sentence is for anyone who wants to know what it actually means, and
          for a screen reader, which gets the explanation rather than a bare
          adjective. */}
      <span className="jo-live-label">{label}</span>
      <span className="sr-only">
        {status === "live"
          ? "This page is connected and updates by itself as mail is read."
          : status === "waiting"
            ? "Connecting to the update stream. Figures on this page may be a moment behind."
            : "Not receiving updates. Figures on this page may be out of date — reload to see the latest."}
      </span>
    </span>
  );
}
