"use client";

import { useEffect, useRef, useState } from "react";

import { SETTINGS_ACCOUNT_PATH } from "../../api";
import { useAuth } from "../../auth";
import type { Candidate } from "../candidates";
import {
  ApiError,
  getCandidateActivities,
  getWhatsappDraft,
  logCandidateActivity,
  type ActivityItem,
} from "../candidates";
import { when } from "../format";

/**
 * The WhatsApp button, its draft modal, and the activity timeline below it.
 *
 * We never send the message ourselves — the recruiter reviews it in the
 * modal, presses Open WhatsApp, and presses send inside WhatsApp itself.
 * Every string in this file says "opened", never "sent", because we have no
 * way of observing whether a send ever happened.
 *
 * There is no dialog primitive anywhere else in this codebase (checked
 * candidate-form.tsx, candidate-history.tsx, the settings panels — all of
 * them are inline cards, not overlays), so the modal below is the simplest
 * correct one: a focus trap, `Escape` to close, focus returned to the button
 * that opened it, and `role="dialog"` + `aria-modal="true"` with a labelled
 * heading.
 */

/** Digits only, no leading `+` — exactly what `web.whatsapp.com/send` wants
 *  in its `phone` parameter. Kept separate from the click handler so the URL
 *  it produces can be asserted on without a browser. */
export function whatsappUrl(phoneE164: string, message: string): string {
  const digits = phoneE164.replace(/\D/g, "");
  return `https://web.whatsapp.com/send?phone=${digits}&text=${encodeURIComponent(message)}`;
}

/** Why the button is inert. `phone_e164` is deliberately left `null` for a
 *  landline as well as for "no number at all", so the reason has to cover
 *  both rather than implying a number is simply missing. */
const NO_NUMBER_REASON = "No WhatsApp-reachable number on file for this candidate.";

/** WhatsApp's glyph, drawn rather than fetched.
 *
 *  An icon in an action row has to be recognised at a glance, and this shape
 *  is recognised by everyone who would ever click it — which is the whole
 *  reason it beats the word. `currentColor` rather than the brand green, so
 *  it inherits the row's own button styling and matches the controls beside
 *  it instead of shouting over them; the shape alone is unmistakable.
 *
 *  `aria-hidden`, because the accessible name lives on the button. A screen
 *  reader that announced both would say "WhatsApp WhatsApp". */
function WhatsappGlyph() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M17.47 14.38c-.3-.15-1.75-.86-2.02-.96-.27-.1-.47-.15-.67.15-.2.3-.77.96-.94 1.16-.17.2-.35.22-.64.08-.3-.15-1.25-.46-2.38-1.47-.88-.78-1.47-1.75-1.65-2.05-.17-.3-.02-.46.13-.6.14-.14.3-.35.45-.53.15-.18.2-.3.3-.5.1-.2.05-.38-.02-.53-.08-.15-.67-1.6-.92-2.2-.24-.58-.49-.5-.67-.51h-.57c-.2 0-.52.07-.79.37-.27.3-1.04 1.02-1.04 2.48s1.06 2.88 1.21 3.08c.15.2 2.1 3.2 5.08 4.49.71.3 1.26.49 1.69.63.71.22 1.36.19 1.87.12.57-.09 1.75-.72 2-1.41.25-.69.25-1.28.17-1.41-.07-.13-.27-.2-.57-.35z" />
      <path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.46 1.32 4.96L2 22l5.25-1.38a9.87 9.87 0 0 0 4.79 1.22h.01c5.46 0 9.91-4.45 9.91-9.91 0-2.65-1.03-5.14-2.9-7.01A9.82 9.82 0 0 0 12.04 2zm0 18.13h-.01a8.2 8.2 0 0 1-4.18-1.15l-.3-.18-3.11.82.83-3.04-.2-.31a8.17 8.17 0 0 1-1.26-4.36c0-4.54 3.7-8.24 8.24-8.24a8.18 8.18 0 0 1 5.82 2.42 8.18 8.18 0 0 1 2.41 5.83c0 4.54-3.7 8.23-8.24 8.23z" />
    </svg>
  );
}

export function WhatsappButton({
  row,
  onLogged,
}: {
  row: Candidate;
  /** Called after the activity POST succeeds, so the timeline below can
   *  refetch. Not called when the popup was blocked — nothing was opened, so
   *  there is nothing new to show. */
  onLogged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const disabled = !row.phone_e164;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="btn btn-secondary btn-icon"
        onClick={() => setOpen(true)}
        disabled={disabled}
        // The icon carries no text, so the name has to come from here — and
        // the same string doubles as the hover tooltip for a sighted user,
        // who otherwise has only the glyph to go on.
        aria-label={disabled ? NO_NUMBER_REASON : `WhatsApp ${row.full_name}`}
        title={disabled ? NO_NUMBER_REASON : `WhatsApp ${row.full_name}`}
        aria-describedby={disabled ? "wa-disabled-reason" : undefined}
      >
        <WhatsappGlyph />
      </button>
      {disabled && (
        <span id="wa-disabled-reason" className="sr-only" hidden>
          {NO_NUMBER_REASON}
        </span>
      )}
      {open && (
        <WhatsappModal
          row={row}
          onLogged={onLogged}
          onClose={() => {
            setOpen(false);
            triggerRef.current?.focus();
          }}
        />
      )}
    </>
  );
}

type DraftState =
  | { status: "loading" }
  | { status: "ready"; phone_e164: string; message: string }
  | { status: "error"; message: string };

function WhatsappModal({
  row,
  onClose,
  onLogged,
}: {
  row: Candidate;
  onClose: () => void;
  onLogged: () => void;
}) {
  const [draft, setDraft] = useState<DraftState>({ status: "loading" });
  const [text, setText] = useState("");
  const [blocked, setBlocked] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  // Not a blocker: the backend already rewrites the message to omit the name
  // entirely rather than fall back to an email (see settings/account.tsx),
  // so a signed-in user with neither name is fine to send, just less
  // personal — this is a nudge toward Settings, not a gate.
  const auth = useAuth();
  const missingName =
    auth.status === "signed-in" && !auth.me.user.preferred_name && !auth.me.user.display_name;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await getWhatsappDraft(row.id);
        if (cancelled) return;
        setDraft({ status: "ready", phone_e164: body.phone_e164, message: body.message });
        setText(body.message);
      } catch (err) {
        if (cancelled) return;
        setDraft({
          status: "error",
          message: err instanceof ApiError ? err.message : "We could not load a draft just now.",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [row.id]);

  // Focus trap + Escape-to-close + return focus on unmount, since this is a
  // hand-built dialog rather than a native <dialog> — see the file header for
  // why there was no existing primitive to reuse.
  useEffect(() => {
    headingRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const node = dialogRef.current;
      if (!node) return;
      const focusable = node.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function openWhatsapp() {
    if (draft.status !== "ready") return;
    const url = whatsappUrl(draft.phone_e164, text);
    // window.open MUST be called synchronously, right here in the click
    // handler, before any `await`. Popup blockers key off the click's call
    // stack; opening inside a `.then()` after awaiting the activity POST
    // below is indistinguishable from opening on a timer, and gets blocked.
    // Do not "tidy" this into `await logCandidateActivity(...); window.open(...)`.
    const win = window.open(url, "expressautomate-whatsapp", "width=460,height=760");
    if (!win) {
      // Nothing opened, so nothing happened — the activity log must stay
      // silent about an event that never occurred, and the recruiter needs
      // the link to click by hand instead.
      setBlocked(url);
      return;
    }
    setBlocked(null);
    void logCandidateActivity(row.id, {
      activity_type: "whatsapp_opened",
      channel: "whatsapp",
      message_text: text,
    })
      .then(onLogged)
      .catch(() => {
        /* WhatsApp is already open in its own tab; a failed log entry is not
           something the recruiter can act on from here. */
      });
    onClose();
  }

  return (
    <div className="wa-overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={dialogRef}
        className="card wa-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="wa-modal-title"
      >
        <h3 id="wa-modal-title" ref={headingRef} tabIndex={-1} className="jo-detail-title">
          WhatsApp {row.full_name}
        </h3>

        {draft.status === "loading" && <p className="body muted">Loading the draft…</p>}

        {draft.status === "error" && (
          <>
            <p className="body jo-detail-error" role="alert">
              {draft.message}
            </p>
            <div className="wa-actions">
              <button type="button" className="btn btn-secondary" onClick={onClose}>
                Cancel
              </button>
            </div>
          </>
        )}

        {draft.status === "ready" && (
          <>
            <p className="body muted">{draft.phone_e164}</p>
            {missingName && (
              <p className="body jo-sub" style={{ marginTop: 8 }}>
                Your name is missing from this message —{" "}
                <a href={SETTINGS_ACCOUNT_PATH}>set it in Account settings</a>.
              </p>
            )}
            <label className="wa-field">
              <span className="row-k">Message</span>
              <textarea
                className="jo-search"
                style={{ minHeight: 160 }}
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
            </label>

            {blocked && (
              <p className="body jo-detail-error" role="alert">
                Your browser blocked the popup.{" "}
                <a href={blocked} target="_blank" rel="noreferrer">
                  Open WhatsApp manually
                </a>
                .
              </p>
            )}

            <div className="wa-actions">
              <button type="button" className="btn btn-primary" onClick={openWhatsapp}>
                Open WhatsApp
              </button>
              <button type="button" className="btn btn-secondary" onClick={onClose}>
                Cancel
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** "WhatsApp opened for <name> by <actor>" — reusing `when` from
 *  `format.tsx` for the timestamp rather than writing a second formatter. */
export function WhatsappActivityTimeline({
  row,
  version,
}: {
  row: Candidate;
  /** Bumped by the caller after a successful log, since the candidate's own
   *  id does not change when a new activity is recorded and would not
   *  otherwise trigger a refetch. */
  version: number;
}) {
  const [items, setItems] = useState<ActivityItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError(null);
    (async () => {
      try {
        const list = await getCandidateActivities(row.id);
        if (!cancelled) setItems(list);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "We could not load activity just now.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [row.id, version]);

  const whatsappOnly = (items ?? []).filter((item) => item.channel === "whatsapp");

  if (items === null && !error) return null;
  if (error) return null;
  if (whatsappOnly.length === 0) return null;

  return (
    <div className="jo-detail-prose">
      <span className="row-k">Activity</span>
      <ul className="wa-timeline">
        {whatsappOnly.map((item) => (
          <li key={item.id} className="body muted">
            WhatsApp opened for {row.full_name} by {item.actor_name} — {when(item.created_at)}
          </li>
        ))}
      </ul>
    </div>
  );
}
