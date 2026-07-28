"use client";

import { useEffect, useRef, useState } from "react";

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
 *
 * The circle is also the control. There is no "Upload photo" button beside it:
 * hovering or focusing the circle reveals a camera overlay, a photo can be
 * dropped straight onto it, and removing one is a small badge that only exists
 * while there is something to remove. The file input is still a real, focusable
 * input behind the overlay rather than a click target synthesised from a div —
 * that is what keeps the whole thing operable from the keyboard.
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
  children,
}: {
  row: Candidate;
  /** Called after an upload or removal succeeds, so the caller can refetch
   *  the candidate record (`avatar_key` changed). */
  onChanged: () => void;
  /** Who this is — the name and current title, rendered beside the circle.
   *  They belong to the panel, not to the avatar, but they sit in the space
   *  the avatar already occupies: a photo with a name next to it reads as a
   *  person, while a photo next to a sentence about uploading reads as a
   *  form control that happens to be round. */
  children?: React.ReactNode;
}) {
  const [photo, setPhoto] = useState<PhotoState>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const shownFor = useRef<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // Re-fetched every time the panel opens on a (possibly different)
  // candidate — the URL is short-lived, so holding one from a previous
  // candidate, or from earlier in this same session, is not an option.
  //
  // Only a change of person clears what is on screen. A re-read of the *same*
  // candidate — which is what an upload triggers — swaps the URL underneath
  // silently, because blanking to the initials for one frame in the middle of
  // adding a photo is the same flicker at a smaller scale.
  useEffect(() => {
    let cancelled = false;
    if (shownFor.current !== row.id) {
      shownFor.current = row.id;
      setPhoto({ status: "loading" });
    }
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
  }, [row.id, row.avatar_key, row.avatar_updated_at]);

  /** The new photo is fetched here rather than waited for from the parent:
   *  `onChanged` only re-reads the candidate record, and the recruiter should
   *  see what they just uploaded the moment it lands, not a frame later. */
  async function upload(file: File) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await uploadCandidateAvatar(row.id, file);
      const avatar = await getCandidateAvatar(row.id);
      if (avatar) setPhoto({ status: "ready", url: avatar.url });
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not upload that photo just now.");
    } finally {
      setBusy(false);
    }
  }

  function onFileChosen(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // lets choosing the same file twice fire onChange again
    if (file) void upload(file);
  }

  function onDrop(event: React.DragEvent) {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    // A drag can carry anything at all — a PDF, a row of text, a folder. The
    // file input filters by `accept`; a drop has to say no for itself.
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setError("That is not an image. Drop a JPEG or PNG instead.");
      return;
    }
    void upload(file);
  }

  async function remove() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteCandidateAvatar(row.id);
      setPhoto({ status: "none" });
      onChanged();
      // The button that was just pressed no longer exists — removal is the
      // only reason it renders. Left alone, focus falls to the body and a
      // keyboard lands back at the top of the document. Hand it to the
      // control that is still there, which is the one they would want next.
      fileRef.current?.focus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We could not remove that photo just now.");
    } finally {
      setBusy(false);
    }
  }

  const initials = initialsFor(row.full_name);
  const background = colorFor(row.full_name);
  const alt = `Photo of ${row.full_name}`;
  const hasPhoto = photo.status === "ready";

  return (
    <div className="jo-detail-avatar">
      <div
        className="ca-shell"
        data-drop={dragging ? "yes" : undefined}
        data-busy={busy ? "yes" : undefined}
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <label className="ca-trigger">
          {hasPhoto ? (
            // A short-lived presigned URL, not an asset `next/image` can
            // optimise or cache — same tradeoff as `telegram-link-panel.tsx`.
            // eslint-disable-next-line @next/next/no-img-element
            <img className="ca-photo" src={photo.url} alt={alt} width={56} height={56} />
          ) : (
            <span
              className="ca-initials"
              role="img"
              aria-label={row.avatar_key ? `${alt} (loading)` : `${row.full_name} has no photo`}
              style={{ background }}
            >
              {initials}
            </span>
          )}
          <span className="ca-overlay" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M4 8h3l1.4-2h7.2L17 8h3v11H4z" strokeLinejoin="round" />
              <circle cx="12" cy="13" r="3.2" />
            </svg>
          </span>
          {/* Visually hidden but not `display: none` — it stays in the tab
              order, so the whole control is reachable without a mouse, and
              the overlay above lights up from `:focus-within`. */}
          <input
            className="ca-file"
            ref={fileRef}
            type="file"
            accept="image/*"
            onChange={onFileChosen}
            disabled={busy}
            aria-label={hasPhoto ? `Replace the photo of ${row.full_name}` : `Add a photo of ${row.full_name}`}
          />
        </label>

        {busy && <span className="ca-spinner" aria-hidden="true" />}

        {hasPhoto && !busy && (
          <button
            type="button"
            className="ca-remove"
            onClick={remove}
            aria-label={`Remove the photo of ${row.full_name}`}
            title="Remove photo"
          >
            <span aria-hidden="true">×</span>
          </button>
        )}
      </div>

      <div className="ca-side">
        {children}
        {/* The invitation is only worth the line while there is nothing to
            show. Once a photo is there the circle explains itself on hover,
            and the space belongs to the person's name. */}
        {(busy || photo.status === "none") && (
          <span className="ca-hint">
            {busy ? "Saving the photo…" : "Drop a photo here, or click the circle."}
          </span>
        )}
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
