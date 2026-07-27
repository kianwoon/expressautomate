"use client";

import { useEffect, useState } from "react";

import { MAILBOX_INGEST_PATH, MAILBOX_PREVIEW_PATH } from "../api";

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
  | { status: "unreadable"; message: string };

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
            message:
              res.status === 403
                ? "Microsoft is no longer letting us read this mailbox. Reconnect it and try again."
                : "We could not reach Microsoft to look at your inbox just now.",
          });
          return;
        }
        const preview = (await res.json()) as Preview;
        setState({ status: "ready", preview });
        // Preselect the smallest commitment. The default someone accepts
        // without reading should be the one that imports nothing.
        setChosen(preview.options[0]?.key ?? null);
      } catch {
        if (!controller.signal.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
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

      <fieldset style={{ marginTop: 32, border: 0, padding: 0 }}>
        <legend className="eyebrow" style={{ padding: 0 }}>
          How far back?
        </legend>
        <div style={{ marginTop: 14, display: "grid", gap: 10, maxWidth: "42ch" }}>
          {preview.options.map((option) => (
            <label
              key={option.key}
              className="card"
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "14px 16px",
                cursor: "pointer",
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
              <span className="muted" style={{ marginLeft: "auto", fontSize: "0.875rem" }}>
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

      <p className="body muted" style={{ marginTop: 16, maxWidth: "62ch", fontSize: "0.875rem" }}>
        Whichever you pick, new mail from now on is read as it arrives. This only decides how much
        of the existing inbox we go back through, and it is asked once.
      </p>

      <button
        className="btn btn-primary"
        style={{ marginTop: 20 }}
        onClick={start}
        disabled={!chosen || starting}
      >
        {starting ? "Starting…" : "Start reading"}
      </button>

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
