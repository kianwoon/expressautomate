"use client";

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

export function StatCards({ me, counts }: { me: Me; counts: Counts }) {
  const { total, in_progress: inProgress, awaiting_extraction: awaiting } = me.mailbox.ingested;
  const last = when(me.mailbox.last_activity);

  return (
    <div className="grid-4 jo-stats">
      <Stat
        value={total.toLocaleString()}
        label="Emails read"
        sub={range(me) ?? "Nothing read yet"}
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
      />
      <Stat
        value={last}
        label="Latest sync"
        sub={last ? "The last thing ingestion did" : "Nothing has happened yet"}
      />
    </div>
  );
}

function Stat({
  value,
  label,
  sub,
  tone,
}: {
  value: string | null;
  label: string;
  sub: string;
  tone?: "attention";
}) {
  return (
    <div className="card jo-stat" data-tone={tone}>
      {/* A null value is an absence, and it is styled as one. The gradient
          numeral is for a figure we actually have. */}
      <div className={value ? "gradient-text jo-stat-v" : "muted jo-stat-v"}>
        {value ?? "Nothing yet"}
      </div>
      <div className="jo-stat-k">{label}</div>
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
