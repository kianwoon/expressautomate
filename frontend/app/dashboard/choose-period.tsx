"use client";

import { useEffect, useState } from "react";

import { CONNECT_MAILBOX_PATH, MAILBOX_INGEST_PATH, MAILBOX_PREVIEW_PATH } from "../api";

/**
 * "How far back should we read?" — asked once, after consent, before anything
 * is imported.
 *
 * The counts are the point. "Last 90 days" is an abstraction until it says
 * 3,905 emails beside it; with the number there, the choice is informed, and
 * without it we would be making the decision on the user's behalf again.
 *
 * Nothing here is estimated. Where Microsoft declines to count a window the
 * option is offered with no number rather than a guess, because a wrong figure
 * is worse than an absent one when someone is deciding on the strength of it.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything is
 * matched against.
 */

type Option = { key: string; label: string; days: number | null; emails: number | null };
type Preview = {
  folder: string;
  total: number | null;
  oldest_received: string | null;
  options: Option[];
};

type State =
  | { status: "loading" }
  | { status: "ready"; preview: Preview }
  // Kept apart from a failed *start*: one means we could not look, the other
  // means we looked and could not begin. They need different words.
  // `reconnectable` is the difference between "the grant is dead" and "we
  // could not reach anyone" — only the first has an action attached.
  | { status: "unreadable"; message: string; reconnectable: boolean };

export function ChoosePeriod({ onStarted }: { onStarted: () => void }) {
  const [state, setState] = useState<State>({ status: "loading" });
  const [chosen, setChosen] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await fetch(MAILBOX_PREVIEW_PATH, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) {
          setState({
            status: "unreadable",
            reconnectable: res.status === 403,
            message:
              res.status === 403
                ? "Microsoft is no longer letting us read this mailbox. Reconnect it and try again."
                : // A 401 is our session expiring, not Microsoft's doing.
                  // Blaming Microsoft for it sends the user to fix the wrong
                  // thing — and the fix, signing in again, is one they know.
                  res.status === 401
                  ? "Your session has expired. Sign in again and we will pick this up where you left it."
                  : "We could not reach Microsoft to look at your inbox just now.",
          });
          return;
        }
        const preview = (await res.json()) as Preview;
        setState({ status: "ready", preview });
        // A day of history, not none.
        //
        // "From now on" was the safest default and the wrong one: it leaves a
        // recruiter looking at an empty dashboard until the next email
        // happens to arrive, with nothing to tell them the thing works. A day
        // is small enough to stay a cheap decision — the count beside it says
        // exactly how many emails it means before anyone commits — and enough
        // that the page has something real in it immediately.
        //
        // Chosen by its window rather than by position, so reordering the
        // options cannot silently change what a new user gets by default.
        const day = preview.options.find((option) => option.days === 1);
        setChosen((day ?? preview.options[0])?.key ?? null);
      } catch {
        if (!controller.signal.aborted) {
          setState({
            status: "unreadable",
            reconnectable: false,
            message: "We could not reach the server.",
          });
        }
      }
    })();
    return () => controller.abort();
  }, []);

  async function start() {
    if (!chosen || starting) return;
    setStarting(true);
    setStartError(null);
    try {
      const res = await fetch(MAILBOX_INGEST_PATH, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ window: chosen }),
      });
      // 409 means it is already running — treat that as success rather than an
      // error, since the state the user wanted is the state they have.
      if (!res.ok && res.status !== 409) {
        setStartError("We could not start reading your mailbox. Try again in a moment.");
        setStarting(false);
        return;
      }
      onStarted();
    } catch {
      setStartError("We could not reach the server.");
      setStarting(false);
    }
  }

  if (state.status === "loading") {
    return (
      <p className="lede" style={{ marginTop: 18 }}>
        Your mailbox is connected. Looking at what is in it — nothing is being read yet.
      </p>
    );
  }

  if (state.status === "unreadable") {
    return (
      <>
        <p className="lede" style={{ marginTop: 18 }}>
          {state.message}
        </p>
        <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
          Nothing has been imported, and nothing will be until you choose a period.
        </p>
        {/* Telling someone to reconnect without giving them the button is
            advice, not a way out. */}
        {state.reconnectable && (
          <a
            className="btn btn-primary"
            rel="nofollow"
            style={{ marginTop: 20, display: "inline-block" }}
            href={CONNECT_MAILBOX_PATH}
          >
            Reconnect your mailbox
          </a>
        )}
      </>
    );
  }

  const { preview } = state;

  return (
    <>
      <p className="lede" style={{ marginTop: 18 }}>
        Your mailbox is connected. Nothing has been read yet —{" "}
        <strong>you choose how far back we go</strong>.
      </p>

      <div className="grid-3" style={{ marginTop: 28 }}>
        <Stat
          value={preview.total === null ? null : preview.total.toLocaleString()}
          label={`in ${preview.folder}`}
          sub="Microsoft's own count"
        />
        <Stat
          value={preview.oldest_received ? day(preview.oldest_received) : null}
          label="oldest email"
          sub={preview.oldest_received ? "The furthest back this inbox goes" : "This inbox is empty"}
        />
        <Stat value={null} label="read so far" sub="Nothing until you press start" />
      </div>

      <fieldset style={{ marginTop: 36, border: 0, padding: 0, margin: 0 }}>
        <legend className="eyebrow" style={{ padding: 0 }}>
          How far back?
        </legend>
        {/* Two columns on anything wider than a phone. As a single narrow
            column under the full-width stat cards it read as an unrelated
            fragment rather than the choice the page is asking for. */}
        <div
          style={{
            marginTop: 16,
            display: "grid",
            gap: 12,
            gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
          }}
        >
          {preview.options.map((option) => (
            <label
              key={option.key}
              className="card"
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "16px 18px",
                cursor: "pointer",
                // The selected option should be obvious without hunting for a
                // filled radio dot. The design token, not its value: a literal
                // here would silently stop matching the day the palette moves.
                outline: chosen === option.key ? "2px solid var(--blue-500)" : "none",
                outlineOffset: -1,
              }}
            >
              <input
                type="radio"
                name="period"
                value={option.key}
                checked={chosen === option.key}
                onChange={() => setChosen(option.key)}
              />
              <span style={{ fontWeight: 600 }}>{option.label}</span>
              <span
                className="muted"
                style={{
                  marginLeft: "auto",
                  fontSize: "0.875rem",
                  whiteSpace: "nowrap",
                }}
              >
                {/* An unknown count says so. Reading "0 emails" off a window
                    Microsoft simply would not count is exactly the mistake
                    this whole step exists to stop. */}
                {option.days === null
                  ? "nothing imported"
                  : option.emails === null
                    ? "count unavailable"
                    : `${option.emails.toLocaleString()} email${option.emails === 1 ? "" : "s"}`}
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      {/* The button and the caveat belong together: the caveat is what makes
          the button safe to press. */}
      <div
        style={{
          marginTop: 24,
          display: "flex",
          alignItems: "center",
          gap: 20,
          flexWrap: "wrap",
        }}
      >
        <button
          className="btn btn-primary"
          onClick={start}
          disabled={!chosen || starting}
        >
          {starting ? "Starting…" : "Start reading"}
        </button>
        <p className="body muted" style={{ margin: 0, maxWidth: "52ch", fontSize: "0.875rem" }}>
          New mail is read as it arrives whichever you pick. This only decides how far back through
          the existing inbox we go, and it is asked once.
        </p>
      </div>

      {startError && (
        <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
          {startError}
        </p>
      )}
    </>
  );
}

function Stat({
  value,
  label,
  sub,
}: {
  value: string | null;
  label: string;
  sub: string;
}) {
  return (
    <div className="card">
      <div
        className={value ? "gradient-text" : "muted"}
        style={{ fontSize: "2.5rem", fontWeight: 700, lineHeight: 1.1 }}
      >
        {value ?? "—"}
      </div>
      <div style={{ marginTop: 6, fontWeight: 600 }}>{label}</div>
      <p className="body muted" style={{ marginTop: 8, fontSize: "0.8125rem" }}>
        {sub}
      </p>
    </div>
  );
}

function day(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
