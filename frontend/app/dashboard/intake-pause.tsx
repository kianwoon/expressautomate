"use client";

import { useCallback, useEffect, useState } from "react";

import {
  MAILBOX_PAUSE_PATH,
  MAILBOX_RESUME_PATH,
  MAILBOX_SETTINGS_PATH,
} from "../api";
import { Dialog } from "./dialog";

import "./intake-pause.css";

/**
 * The intake switch: is this recruiter's own mailbox feeding the pipeline?
 *
 * Deliberately not part of `LiveLight`, which sits beside it. That light is a
 * read-only report of whether the SSE stream is delivering — making it
 * clickable would give one word two unrelated meanings, and a paused mailbox
 * would read as "this page is broken" rather than "my intake is off". This is
 * its own control with its own words.
 *
 * The failure mode of this feature is forgetting it is off, so the paused
 * state says since when and is styled to stay conspicuous rather than recede
 * into the header. Never colour alone — the state is in words, as `LiveLight`
 * already establishes.
 *
 * Resuming asks once, in plain language, before acting: mail that arrived
 * during the pause is never picked up, and that is the one fact someone must
 * not learn afterwards.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the
 * page, not a list anything is matched against.
 */

type State =
  | { status: "loading" }
  // No mailbox connected (or the session lapsed): there is nothing to pause,
  // so the control renders nothing rather than a switch wired to a 404.
  | { status: "absent" }
  | { status: "ready"; pausedAt: string | null };

export function IntakePause() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback((signal?: AbortSignal) => {
    return (async () => {
      try {
        const res = await fetch(MAILBOX_SETTINGS_PATH, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal,
        });
        if (!res.ok) {
          setState({ status: "absent" });
          return;
        }
        const body = (await res.json()) as { ingest_paused_at: string | null };
        setState({ status: "ready", pausedAt: body.ingest_paused_at ?? null });
      } catch {
        if (!signal?.aborted) setState({ status: "absent" });
      }
    })();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function pause() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(MAILBOX_PAUSE_PATH, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) {
        setError("We could not pause intake just now. Try again in a moment.");
        return;
      }
      const body = (await res.json()) as { paused_at: string };
      setState({ status: "ready", pausedAt: body.paused_at });
    } catch {
      setError("We could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  async function resume() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(MAILBOX_RESUME_PATH, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) {
        // The server's words where it has better ones — a resume that could
        // not reach Microsoft says the mailbox is still paused, and this
        // pill must not claim otherwise.
        const detail = (await res.json().catch(() => null))?.detail;
        setError(
          typeof detail === "string"
            ? detail
            : "We could not resume intake just now. Your mailbox is still paused.",
        );
        return;
      }
      setState({ status: "ready", pausedAt: null });
      setConfirming(false);
    } catch {
      setError("We could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  if (state.status !== "ready") return null;

  const paused = state.pausedAt !== null;

  return (
    <>
      <button
        type="button"
        className="jo-intake"
        data-state={paused ? "paused" : "on"}
        disabled={busy}
        onClick={() => (paused ? setConfirming(true) : void pause())}
      >
        <span className="jo-intake-dot" aria-hidden="true" />
        <span>{paused ? `Intake paused since ${day(state.pausedAt!)}` : "Intake on"}</span>
        {/* The visible label carries the state; this carries what clicking
            does, which a sighted user infers from it being a button. */}
        <span className="sr-only">
          {paused
            ? "Press to resume reading your inbox from now."
            : "Press to pause reading your inbox."}
        </span>
      </button>
      {error && (
        <span className="jo-intake-error" role="alert">
          {error}
        </span>
      )}
      {confirming && (
        <Dialog
          title="Resume reading your inbox?"
          titleId="intake-resume-title"
          onClose={() => setConfirming(false)}
        >
          <p className="body" style={{ maxWidth: "48ch" }}>
            Reading starts again from <strong>right now</strong>. Email that arrived while intake
            was paused is <strong>not</strong> read and will not be picked up later — if any of it
            matters, handle it in your inbox directly.
          </p>
          <div style={{ display: "flex", gap: 12, marginTop: 20, flexWrap: "wrap" }}>
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() => void resume()}
            >
              {busy ? "Resuming…" : "Resume from now"}
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={busy}
              onClick={() => setConfirming(false)}
            >
              Stay paused
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}

function day(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
  });
}
