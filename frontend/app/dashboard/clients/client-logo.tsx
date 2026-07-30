"use client";

import { useEffect, useRef, useState } from "react";

import type { Client } from "../clients";
import { deleteClientLogo, getClientLogo, uploadClientLogo } from "../clients";

/**
 * The client's logo, or the initials that stand in for one.
 *
 * Mirrors `candidate-avatar.tsx` closely, with one deliberate difference: a
 * logo is a wordmark, not a face, so it is CONTAINED in a rounded square
 * rather than cropped to a circle. The backend already letterboxes it onto a
 * transparent square canvas for exactly this reason (see
 * `clients_logo.py`) — a circular mask here would crop it a second time and
 * undo that work.
 *
 * `GET /clients/{id}/logo` returns a presigned URL good for roughly five
 * minutes, not the file, so it is re-fetched every time the panel opens (or
 * the client id changes) and is never written anywhere longer-lived than
 * this component's own state — no `localStorage`, no caching past this
 * component's lifetime.
 *
 * The mark itself is the control: hovering or focusing it reveals a camera
 * overlay over a real, focusable `<input type="file">`, so the whole thing
 * stays operable from the keyboard with no separate "Upload" button.
 */

const LOGO_COLORS = [
  "#5b6ee1",
  "#e15b8f",
  "#2fa88a",
  "#c77f2f",
  "#8a5be1",
  "#2f8fc7",
  "#c74f4f",
  "#4f9e4f",
];

/** Deterministic, not random: the same name always lands on the same colour. */
function colorFor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) | 0;
  }
  return LOGO_COLORS[Math.abs(hash) % LOGO_COLORS.length];
}

function initialsFor(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

type LogoState =
  | { status: "loading" }
  | { status: "none" }
  | { status: "ready"; url: string }
  | { status: "unreadable" };

export function ClientLogo({
  client,
  onChange,
}: {
  client: Client;
  /** Called after an upload or removal succeeds, so the caller can refetch
   *  the client record (`logo_key` changed). */
  onChange: () => void;
}) {
  const [logo, setLogo] = useState<LogoState>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const shownFor = useRef<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // Re-fetched on mount and whenever the client id changes — the URL is
  // short-lived, so holding one from a previous client, or from earlier in
  // this same session, is not an option.
  //
  // Only a change of client clears what is on screen. A re-read of the
  // *same* client (which is what an upload triggers) swaps the URL
  // underneath silently, so uploading does not flash back to initials first.
  useEffect(() => {
    let cancelled = false;
    if (shownFor.current !== client.id) {
      shownFor.current = client.id;
      setLogo({ status: "loading" });
    }
    // `logo_key` is already on the client record, so a client known to have
    // none does not need a round trip to learn that — it would just be a
    // 404 spent to confirm what the record already said (§15: absence is
    // "not mentioned", not something to go verify).
    if (!client.logo_key) {
      setLogo({ status: "none" });
      return;
    }
    (async () => {
      try {
        const url = await getClientLogo(client.id);
        if (cancelled) return;
        setLogo(url ? { status: "ready", url: url.url } : { status: "none" });
      } catch {
        if (!cancelled) setLogo({ status: "unreadable" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client.id, client.logo_key, client.logo_updated_at]);

  async function upload(file: File) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await uploadClientLogo(client.id, file);
      const url = await getClientLogo(client.id);
      if (url) setLogo({ status: "ready", url: url.url });
      onChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not upload that logo just now.");
    } finally {
      setBusy(false);
    }
  }

  function onFileChosen(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // lets choosing the same file twice fire onChange again
    if (file) void upload(file);
  }

  async function remove() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteClientLogo(client.id);
      setLogo({ status: "none" });
      onChange();
      // The button that was just pressed no longer exists — hand focus to
      // the control that is still there.
      fileRef.current?.focus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not remove that logo just now.");
    } finally {
      setBusy(false);
    }
  }

  const initials = initialsFor(client.name);
  const background = colorFor(client.name);
  const alt = `Logo of ${client.name}`;
  const hasLogo = logo.status === "ready";

  return (
    <div className="jo-detail-logo">
      <div className="cl-logo-shell" data-busy={busy ? "yes" : undefined}>
        <label className="cl-logo-trigger">
          {hasLogo ? (
            // A short-lived presigned URL, not an asset `next/image` can
            // optimise or cache — same tradeoff as `candidate-avatar.tsx`.
            // eslint-disable-next-line @next/next/no-img-element
            <img className="cl-logo-photo" src={logo.url} alt={alt} width={56} height={56} />
          ) : (
            <span
              className="cl-logo-initials"
              role="img"
              aria-label={client.logo_key ? `${alt} (loading)` : `${client.name} has no logo`}
              style={{ background }}
            >
              {initials}
            </span>
          )}
          <span className="cl-logo-overlay" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M4 8h3l1.4-2h7.2L17 8h3v11H4z" strokeLinejoin="round" />
              <circle cx="12" cy="13" r="3.2" />
            </svg>
          </span>
          {/* Visually hidden but not `display: none` — it stays in the tab
              order, so the whole control is reachable without a mouse. */}
          <input
            className="cl-logo-file"
            ref={fileRef}
            type="file"
            accept="image/*"
            onChange={onFileChosen}
            disabled={busy}
            aria-label={hasLogo ? `Replace the logo of ${client.name}` : `Add a logo of ${client.name}`}
          />
        </label>

        {busy && <span className="cl-logo-spinner" aria-hidden="true" />}

        {hasLogo && !busy && (
          <button
            type="button"
            className="cl-logo-remove"
            onClick={remove}
            aria-label={`Remove the logo of ${client.name}`}
            title="Remove logo"
          >
            <span aria-hidden="true">×</span>
          </button>
        )}
      </div>

      {logo.status === "unreadable" && (
        <span className="body muted">Could not load the logo just now.</span>
      )}
      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
