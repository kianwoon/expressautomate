"use client";

import { useEffect, useState } from "react";

import type { Candidate } from "../candidates";
import { deleteCandidateAvatar, getCandidateAvatar, uploadCandidateAvatar } from "../candidates";

/**
 * The candidate's photo, or the initials that stand in for one.
 *
 * Lives only inside `candidate-panel.tsx` (the modal / detail panel) — not
 * the candidates table. Of the seeded candidates none has a photo yet, so
 * the initials fallback is what almost every row shows, and it must render
 * with no network request and no broken-image flash: it is derived from
 * `full_name` alone, synchronously, before any fetch has a chance to answer.
 *
 * The photo itself is never that simple. `GET /candidates/{id}/avatar`
 * returns a presigned URL good for roughly five minutes, not the file, so it
 * is re-fetched every time the panel opens (`row.id` in the effect
 * dependency) and is never written anywhere longer-lived than this
 * component's own state — no `localStorage`, no caching past the panel's
 * lifetime, since a stale link is a link that will simply stop working.
 */

const AVATAR_COLORS = [
  "#5b6ee1",
  "#e15b8f",
  "#2fa88a",
  "#c77f2f",
  "#8a5be1",
  "#2f8fc7",
  "#c74f4f",
  "#4f9e4f",
];

/** Deterministic, not random: the same name always lands on the same colour,
 *  in this session and the next, on this browser and any other. */
function colorFor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) | 0;
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function initialsFor(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

type PhotoState =
  | { status: "loading" }
  | { status: "none" }
  | { status: "ready"; url: string }
  | { status: "unreadable" };

export function CandidateAvatar({
  row,
  onChanged,
}: {
  row: Candidate;
  /** Called after an upload or removal succeeds, so the caller can refetch
   *  the candidate record (`avatar_key` changed). */
  onChanged: () => void;
}) {
  const [photo, setPhoto] = useState<PhotoState>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-fetched every time the panel opens on a (possibly different)
  // candidate — the URL is short-lived, so holding one from a previous
  // candidate, or from earlier in this same session, is not an option.
  useEffect(() => {
    let cancelled = false;
    setPhoto({ status: "loading" });
    (async () => {
      try {
        const avatar = await getCandidateAvatar(row.id);
        if (cancelled) return;
        setPhoto(avatar ? { status: "ready", url: avatar.url } : { status: "none" });
      } catch {
        if (!cancelled) setPhoto({ status: "unreadable" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [row.id, row.avatar_key]);

  async function onFileChosen(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // lets choosing the same file twice fire onChange again
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      await uploadCandidateAvatar(row.id, file);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not upload that photo just now.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteCandidateAvatar(row.id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not remove that photo just now.");
    } finally {
      setBusy(false);
    }
  }

  const initials = initialsFor(row.full_name);
  const background = colorFor(row.full_name);
  const alt = `Photo of ${row.full_name}`;

  return (
    <div className="jo-detail-avatar" style={{ display: "flex", alignItems: "center", gap: 12 }}>
      {photo.status === "ready" ? (
        // A short-lived presigned URL, not an asset `next/image` can
        // optimise or cache — same tradeoff as `telegram-link-panel.tsx`.
        <img
          src={photo.url}
          alt={alt}
          width={56}
          height={56}
          style={{ width: 56, height: 56, borderRadius: "50%", objectFit: "cover" }}
        />
      ) : (
        <span
          aria-label={row.avatar_key ? `${alt} (loading)` : `${row.full_name} has no photo`}
          style={{
            width: 56,
            height: 56,
            borderRadius: "50%",
            background,
            color: "#fff",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontWeight: 600,
            fontSize: 18,
          }}
        >
          {initials}
        </span>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <div style={{ display: "flex", gap: 8 }}>
          <label className="btn btn-secondary" style={{ cursor: busy ? "default" : "pointer" }}>
            {busy ? "Working…" : "Upload photo"}
            <input
              type="file"
              accept="image/*"
              onChange={onFileChosen}
              disabled={busy}
              aria-label="Upload a candidate photo"
              style={{
                position: "absolute",
                width: 1,
                height: 1,
                padding: 0,
                margin: -1,
                overflow: "hidden",
                clip: "rect(0,0,0,0)",
                border: 0,
              }}
            />
          </label>
          {photo.status === "ready" && (
            <button type="button" className="btn btn-secondary" onClick={remove} disabled={busy}>
              Remove
            </button>
          )}
        </div>
        {photo.status === "unreadable" && (
          <span className="body muted">Could not load the photo just now.</span>
        )}
        {error && (
          <p className="body jo-detail-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
