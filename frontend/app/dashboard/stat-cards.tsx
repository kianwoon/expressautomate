"use client";

import { useEffect, useState } from "react";

import type { Me } from "../auth";
import { when } from "./format";
import type { Counts } from "./opportunities";

/**
 * The four numbers a recruiter opening this page wants: how much mail was
 * read, how much of it needs them, how many vacancies came out, and whether
 * any of it is current.
 *
 * There are no sparklines. The design carried one under each figure, and we do
 * not store a per-day series for any of these — the only honest options were
 * to plot nothing or to draw a shape that means nothing. A decorative squiggle
 * on a card that also shows a real count is read as history, and inventing
 * history in a product whose entire pitch is that it does not invent things is
 * not a trade worth making for a bit of texture.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

/** Re-render on a timer so the relative time on the sync card stays true. */
function useTick(seconds: number): void {
  const [, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), seconds * 1000);
    return () => clearInterval(id);
  }, [seconds]);
}

/**
 * How long ago, in words.
 *
 * Returns null rather than "just now" for an absent timestamp: nothing having
 * happened and something having happened a moment ago are different facts, and
 * the card styles an absence differently from a figure.
 */
function ago(iso: string | null): string | null {
  if (!iso) return null;
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "Just now";
  // Floor, not round. Elapsed time only ever counts what has actually passed:
  // rounding said "2 min ago" at 91 seconds and "2 hr ago" at 90 minutes,
  // overstating staleness by up to half a unit on the one card whose job is to
  // tell you whether the data is fresh.
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return minutes === 1 ? "1 min ago" : `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return hours === 1 ? "1 hr ago" : `${hours} hr ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

export function StatCards({ me, counts }: { me: Me; counts: Counts }) {
  const { total, in_progress: inProgress, awaiting_extraction: awaiting } = me.mailbox.ingested;
  const last = when(me.mailbox.last_activity);
  useTick(30);

  return (
    <div className="grid-4 jo-stats">
      <Stat
        value={total.toLocaleString()}
        label="Emails read"
        sub={range(me) ?? "Nothing read yet"}
        glyph="mail"
        accent="blue"
      />
      <Stat
        value={counts.needs_review.toLocaleString()}
        label="Waiting for review"
        // A zero here is good news and should read as good news, not as an
        // empty state.
        sub={
          counts.needs_review === 0
            ? "Nothing is waiting on you"
            : "Job orders where the email left too much out"
        }
        tone={counts.needs_review > 0 ? "attention" : undefined}
        glyph="clock"
        accent="amber"
      />
      <Stat
        value={counts.all.toLocaleString()}
        label="Job orders found"
        // Vacancies, not emails: one email can advertise six roles, and one
        // did — which is how a headline of 3 once sat above a table of 7.
        sub={
          counts.all === 0
            ? inProgress + awaiting > 0
              ? "Still reading"
              : "Nothing extracted yet"
            : "Listed below"
        }
        glyph="briefcase"
        accent="teal"
      />
      {/* "4 minutes ago" answers the question this card is asked — is any of
          this current? — which a timestamp makes the reader work out. The
          exact moment stays underneath, because "recently" is not something
          to take on trust when a mailbox has stopped.

          It ticks. A relative time rendered once quietly ages into a lie on a
          page left open, and this is the one card whose entire job is to say
          whether the data is stale. */}
      <Stat
        value={ago(me.mailbox.last_activity)}
        label="Latest sync"
        sub={last ?? "Nothing has happened yet"}
        glyph="sync"
        accent="blue"
      />
    </div>
  );
}

/**
 * The four glyphs.
 *
 * Line art at one weight, sharing the card's accent colour, so the row reads
 * as one set of four rather than four unrelated boxes — which is what four
 * bare numerals in four bare cards looked like. Inline SVG rather than an icon
 * font: three paths cost less than a webfont request, and nothing here should
 * render as a missing-glyph box if that request fails.
 */
const GLYPH = {
  mail: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2.5" />
      <path d="m3.6 6.8 7.3 5.2a2 2 0 0 0 2.2 0l7.3-5.2" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.4V12l3 1.8" />
    </>
  ),
  briefcase: (
    <>
      <rect x="3" y="7.5" width="18" height="12" rx="2.5" />
      <path d="M9 7.5V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v1.5M3 12.5h18" />
    </>
  ),
  sync: (
    <>
      <path d="M20 12a8 8 0 1 1-2.6-5.9" />
      <path d="M20 4.2V9h-4.8" />
    </>
  ),
  // Not typed as Record<string, …>: that widens the key to `string`, so a
  // misspelt glyph name compiles and renders an empty tinted square.
} as const;

function Stat({
  value,
  label,
  sub,
  glyph,
  accent,
  tone,
}: {
  value: string | null;
  label: string;
  sub: string;
  glyph: keyof typeof GLYPH;
  accent: "blue" | "amber" | "teal";
  tone?: "attention";
}) {
  return (
    <div className="card jo-stat" data-tone={tone} data-accent={accent}>
      <span className="jo-stat-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
          strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          {GLYPH[glyph]}
        </svg>
      </span>
      {/* The label comes first in the DOM and reads first on the card. A
          figure means nothing until you know what it counts, and the previous
          order made every card a number you had to look underneath to
          understand. */}
      <div className="jo-stat-k">{label}</div>
      {/* A null value is an absence, and it is styled as one. The gradient
          numeral is for a figure we actually have. */}
      <div className={value ? "gradient-text jo-stat-v" : "muted jo-stat-v"}>
        {value ?? "Nothing yet"}
      </div>
      <p className="body muted jo-stat-sub">{sub}</p>
    </div>
  );
}

function range(me: Me): string | null {
  const { oldest_received: oldest, newest_received: newest } = me.mailbox;
  if (!oldest || !newest) return null;
  const from = day(oldest);
  const to = day(newest);
  return from === to ? from : `${from} to ${to}`;
}

function day(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/**
 * The bell.
 *
 * It is not a notification system and must not imply one: there is no unread
 * state, nothing is pushed, and clicking it does not mark anything as seen. It
 * is a shortcut to one filter, showing one number that is already on the page.
 * Which is why it disappears entirely at zero rather than sitting there with a
 * dot — an always-present bell trains people to expect notifications we do not
 * send.
 */
export function ReviewBell({ count, onOpen }: { count: number; onOpen: () => void }) {
  if (count === 0) return null;
  return (
    <button type="button" className="jo-bell" onClick={onOpen}>
      <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" fill="none">
        <path
          d="M6 9a6 6 0 1 1 12 0c0 3.5.8 5 1.6 5.8.4.4.1 1.2-.5 1.2H4.9c-.6 0-.9-.8-.5-1.2C5.2 14 6 12.5 6 9Z"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
        <path d="M9.5 19a2.5 2.5 0 0 0 5 0" stroke="currentColor" strokeWidth="1.6" />
      </svg>
      <span className="jo-bell-n">{count.toLocaleString()}</span>
      <span>waiting for review</span>
    </button>
  );
}
