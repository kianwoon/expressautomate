"use client";

import { useEffect, useRef, useState } from "react";

import { SETTINGS_ACCOUNT_PATH, SETTINGS_WHATSAPP_PATH } from "../../api";
import { useAuth } from "../../auth";
import type { Candidate } from "../candidates";
import {
  ApiError,
  getCandidateActivities,
  getWaSessionStatus,
  getWhatsappDraft,
  logCandidateActivity,
  sendCandidateWhatsapp,
  WhatsappSendError,
  type ActivityItem,
  type WaSessionStatus,
} from "../candidates";
import { when } from "../format";

/**
 * The WhatsApp button, its draft modal, and the activity timeline below it.
 *
 * Two ways out of this modal now. When the recruiter's own WhatsApp is
 * linked (Settings → WhatsApp, `whatsapp-panel.tsx`) and its session is
 * `connected`, `Send` submits the draft through the server, which relays it
 * over that linked session. Every other session state — including "we
 * haven't checked yet" — falls back to the original popup path: `Open
 * WhatsApp` opens `web.whatsapp.com/send` and the recruiter presses send
 * themselves inside WhatsApp. That popup keeps working unconditionally; a
 * blocked popup or a failed send always leaves it on screen as the way out.
 *
 * "sent" here means WhatsApp accepted the message from the API, nothing
 * more — not delivered, not read (§15: don't claim what we don't know). The
 * popup path still says "opened", because that is all a `window.open` call
 * can ever tell us.
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
        className="btn btn-icon wa-btn"
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

/** Why `Send` is or isn't offered. `unknown` is the state while the session
 *  fetch is still in flight — deliberately treated the same as "not
 *  connected" (popup only), per the build note: a modal that hesitates on a
 *  disabled Send is worse than one that just works via the popup. */
type SendAvailability = "unknown" | WaSessionStatus;

/** One sentence a recruiter can act on for a failed send, plus which of the
 *  two fallback links (if any) belongs under it. Mirrors the honesty rules in
 *  `whatsapp-panel.tsx`: never call `gateway_unreachable` "disconnected".
 *
 * - `kind: "no_number"` (422) — the fix is on the candidate record. Settings
 *   → WhatsApp would be the wrong advice, so neither fallback link is shown;
 *   `Open WhatsApp` itself is also useless with no number, which is exactly
 *   why the button is disabled at the top of this file when `phone_e164` is
 *   null — this modal cannot even have opened in that state normally, but
 *   the copy still has to be correct if a session change races the open.
 * - `kind: "rate_limited"` (429) — the daily cap, not a link problem. Offer
 *   the popup, not Settings.
 * - `kind: "session"` (409) — branches on `session_status` for the reason
 *   above; `pointToSettings` is true only where a relink or a wait in
 *   Settings is the actual fix. */
function sendFailureCopy(
  kind: "session" | "rate_limited" | "no_number",
  sessionStatus: string | null,
  serverDetail: string,
): { text: string; pointToSettings: boolean; offerPopup: boolean } {
  if (kind === "no_number") {
    return { text: serverDetail, pointToSettings: false, offerPopup: false };
  }
  if (kind === "rate_limited") {
    return { text: serverDetail, pointToSettings: false, offerPopup: true };
  }
  switch (sessionStatus) {
    case "gateway_unreachable":
      return {
        text: "We can't tell right now whether your WhatsApp is linked — this isn't the same as being disconnected. Try again in a moment.",
        pointToSettings: false,
        offerPopup: true,
      };
    case "logged_out":
      return { text: "WhatsApp ended your link.", pointToSettings: true, offerPopup: true };
    case "disconnected":
      return { text: "Your WhatsApp isn't linked yet.", pointToSettings: true, offerPopup: true };
    case "reconnecting":
      return {
        text: "Your WhatsApp is reconnecting after a restart — this usually clears within a minute. Try again shortly.",
        pointToSettings: false,
        offerPopup: true,
      };
    case "pairing":
      return {
        text: "Your WhatsApp link is still pairing.",
        pointToSettings: true,
        offerPopup: true,
      };
    default:
      return { text: serverDetail, pointToSettings: false, offerPopup: true };
  }
}

/** A fresh id per composed message, not per click — regenerated only when the
 *  draft is (re)loaded or the recruiter edits the text, so a retry of the
 *  *same* message reuses the *same* id and the server's unique index turns a
 *  double-click or a client-side timeout-then-retry into a no-op instead of a
 *  second message reaching the candidate. */
function newClientRequestId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `wa-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

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
  const [availability, setAvailability] = useState<SendAvailability>("unknown");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<{
    text: string;
    pointToSettings: boolean;
    offerPopup: boolean;
  } | null>(null);
  const [clientRequestId, setClientRequestId] = useState(() => newClientRequestId());
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
        // A freshly loaded draft is a new message, distinct from whatever
        // was retried before this modal opened.
        setClientRequestId(newClientRequestId());
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

  // Session status is fetched once, when the modal opens — not polled and
  // not tied to the SSE stream the settings panel uses, because this modal
  // is short-lived and only needs one answer: is Send offerable right now.
  // Left at "unknown" (popup-only) on any failure, per the build note.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const status = await getWaSessionStatus();
      if (!cancelled) setAvailability(status);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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

  async function sendNow() {
    // `sending` alone is the guard against a double-submit from this button;
    // `clientRequestId` is the guard against the harder case — a request
    // that timed out client-side after the gateway already accepted it — so
    // both stay in place even though either would stop a same-session
    // double-click.
    if (draft.status !== "ready" || sending) return;
    setSending(true);
    setSendError(null);
    try {
      const result = await sendCandidateWhatsapp(row.id, text, clientRequestId);
      // The server already logged the activity — unlike the popup path,
      // there is nothing left to POST here.
      onLogged();

      if (result.status === "unknown") {
        // A 200 whose status is `unknown` is not a success. The message was
        // dispatched and the answer never came back, so closing the modal the
        // way a send does would tell the recruiter it worked. Leaving it open
        // with the truth is the only option that does not push them toward
        // sending it a second time to a candidate who may already have it.
        setSendError({
          text:
            "We sent this but did not hear back, so we cannot say whether it arrived. " +
            "Check WhatsApp on your phone before sending it again.",
          pointToSettings: false,
          offerPopup: false,
        });
        return;
      }

      // Close and let the timeline refetch pick up the new "sent" row.
      onClose();
    } catch (err) {
      // A failed send is not a dead end: the modal stays open, says what the
      // server said, and `Open WhatsApp` is still there below when offered.
      if (err instanceof WhatsappSendError) {
        setSendError(sendFailureCopy(err.kind, err.session_status, err.message));
        if (err.kind === "session" && err.session_status) {
          setAvailability(err.session_status as WaSessionStatus);
        }
      } else {
        setSendError({
          text: err instanceof ApiError ? err.message : "We could not send that just now.",
          pointToSettings: false,
          offerPopup: true,
        });
      }
    } finally {
      setSending(false);
    }
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
                onChange={(e) => {
                  setText(e.target.value);
                  // An edit makes this a different message than whatever was
                  // last attempted — a retry of the *edited* text must not
                  // dedupe against a send the server has no reason to treat
                  // as the same request.
                  setClientRequestId(newClientRequestId());
                }}
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

            {sendError && (
              <p className="body jo-detail-error" role="alert">
                {sendError.text}
                {sendError.pointToSettings && (
                  <>
                    {" "}
                    <a href={SETTINGS_WHATSAPP_PATH}>Fix this in Settings → WhatsApp</a>, or use Open
                    WhatsApp below.
                  </>
                )}
                {!sendError.pointToSettings && sendError.offerPopup && (
                  <> Use Open WhatsApp below.</>
                )}
              </p>
            )}

            <div className="wa-actions">
              {availability === "connected" ? (
                <>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => void sendNow()}
                    disabled={sending}
                  >
                    {sending ? "Sending…" : "Send"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    onClick={openWhatsapp}
                    disabled={sending}
                  >
                    Open WhatsApp instead
                  </button>
                </>
              ) : (
                <button type="button" className="btn btn-primary" onClick={openWhatsapp}>
                  Open WhatsApp
                </button>
              )}
              <button type="button" className="btn btn-secondary" onClick={onClose} disabled={sending}>
                Cancel
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** One line of the timeline, from an activity's own `status` — the same
 *  field the server already sets on every row, not something inferred from
 *  `activity_type`.
 *
 * - `sent` says only that WhatsApp accepted it (§15: not delivered, not
 *   read).
 * - `failed` must stay visible rather than being filtered out — a silent
 *   failure is the one outcome a recruiter cannot recover from unnoticed.
 * - `unknown` is the one that is easiest to get wrong: a dispatch whose
 *   outcome was never learned (a timeout, a lost reply) is neither a success
 *   nor a failure, and rendering it as either is a §15 breach in whichever
 *   direction is wrong. The copy says exactly that and tells the recruiter
 *   to check WhatsApp before resending, rather than guessing on their
 *   behalf.
 * - anything else (`opened`, or a status this file predates) keeps the
 *   original "opened" wording, since the popup path never claims more than
 *   that either. */
function whatsappActivityLine(item: ActivityItem, fullName: string): string {
  if (item.status === "sent") {
    return `WhatsApp sent to ${fullName} by ${item.actor_name}`;
  }
  if (item.status === "failed") {
    return `WhatsApp send to ${fullName} failed (by ${item.actor_name})`;
  }
  if (item.status === "unknown") {
    return `WhatsApp send to ${fullName} by ${item.actor_name} — outcome unknown, check WhatsApp before resending`;
  }
  return `WhatsApp opened for ${fullName} by ${item.actor_name}`;
}

/** One row per activity — `opened`, `sent`, `failed`, or `unknown` — reusing
 *  `when` from `format.tsx` for the timestamp rather than writing a second
 *  formatter. A refusal (no linked session, no candidate number) produces no
 *  row at all: nothing was attempted, so there is nothing to log. */
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
          <li
            key={item.id}
            // `failed` gets the same error styling as the modal's own error
            // text, rather than blending into the muted rows around it — a
            // failed send that reads like every other row is a failure a
            // recruiter can miss. `unknown` gets its own warning treatment:
            // it must look neither as settled as a plain muted row nor as
            // alarming as a confirmed failure, because it is neither.
            className={
              item.status === "failed"
                ? "body jo-detail-error"
                : item.status === "unknown"
                  ? "body jo-sub"
                  : "body muted"
            }
            role={item.status === "failed" || item.status === "unknown" ? "alert" : undefined}
          >
            {whatsappActivityLine(item, row.full_name)} — {when(item.created_at)}
          </li>
        ))}
      </ul>
    </div>
  );
}
