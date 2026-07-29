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

/** WhatsApp's mark, in its own colours.
 *
 *  An icon in an action row has to be recognised at a glance, and this shape
 *  is recognised by everyone who would ever click it — which is the whole
 *  reason it beats the word. It was drawn in `currentColor` first, to sit
 *  quietly with the controls beside it; the brand tile replaced that on
 *  request. The green is the point rather than a cost: it is the half-second
 *  of recognition a monochrome outline spends, and this is the only control
 *  in the row that leaves the app for somewhere else.
 *
 *  A file rather than inline paths, because the same mark now appears at two
 *  sizes in two components, and the browser fetches it once for both.
 *
 *  `aria-hidden`, because the accessible name lives on the button and the
 *  modal's heading already reads "WhatsApp". A screen reader that announced
 *  both would say "WhatsApp WhatsApp". */
function WhatsappGlyph({ size = 16 }: { size?: number }) {
  return (
    <img
      className="wa-mark"
      src="/whatsapp.svg"
      alt=""
      aria-hidden="true"
      width={size}
      height={size}
    />
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
        <h3 id="wa-modal-title" ref={headingRef} tabIndex={-1} className="jo-detail-title wa-title">
          <WhatsappGlyph size={24} />
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
