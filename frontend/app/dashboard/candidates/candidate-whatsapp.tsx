"use client";

import { useCallback, useEffect, useRef, useState } from "react";

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
import { Dialog } from "../dialog";
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
 * - `kind: "rate_limited"` (429) — not a link problem, but two genuinely
 *   different refusals, told apart by `limit`. `"daily"` is done for today:
 *   the reset is UTC midnight, which is 8am in Singapore, so "tomorrow" would
 *   be wrong for most of the working day — the copy names the server's own
 *   `retry_after_seconds` instead of a day-of-week guess, and still argues
 *   for the popup, since a wait that long is rarely worth it. `"interval"`
 *   clears itself in seconds — telling them to abandon Send for the popup
 *   over a wait that short would be worse advice than telling them to wait,
 *   so the copy leads with the number and treats the popup as optional.
 *   Either way `Open WhatsApp instead` stays available below; this only
 *   changes what the text argues for. `serverDetail` is the fallback for a
 *   429 that reached here without a `limit` (should not happen once the
 *   backend contract is live, but a response the client cannot classify must
 *   still show *something*).
 * - `kind: "risk_not_acknowledged"` (409, no `session_status` — the session
 *   may be perfectly `connected`) — the recruiter has not ticked the risk
 *   checkbox in Settings → WhatsApp. Checked before the `session_status`
 *   branching below, since the two reasons for a 409 do not overlap. The
 *   popup is deliberately *not* offered here: every other refusal offers it
 *   because the message still deserves to go out some way, but this refusal
 *   exists specifically to make sure the recruiter has seen the risk notice
 *   first, and a popup escape hatch would defeat that. The notice itself is
 *   not repeated here — it lives once, in Settings, rendered from the server.
 * - `kind: "session"` (409) — branches on `session_status` for the reason
 *   above; `pointToSettings` is true only where a relink or a wait in
 *   Settings is the actual fix. */
function sendFailureCopy(
  kind: "session" | "rate_limited" | "no_number" | "risk_not_acknowledged",
  sessionStatus: string | null,
  serverDetail: string,
  limit?: "daily" | "interval" | null,
  retryAfterSeconds?: number | null,
): { text: string; pointToSettings: boolean; offerPopup: boolean } {
  if (kind === "no_number") {
    return { text: serverDetail, pointToSettings: false, offerPopup: false };
  }
  if (kind === "risk_not_acknowledged") {
    return {
      text: `${serverDetail} You need to acknowledge that risk in Settings → WhatsApp before sending — there's no way around this one.`,
      pointToSettings: true,
      offerPopup: false,
    };
  }
  if (kind === "rate_limited") {
    if (limit === "daily") {
      const wait =
        typeof retryAfterSeconds === "number"
          ? ` It resets in about ${describeWait(retryAfterSeconds)}.`
          : "";
      return {
        text: `${serverDetail}${wait} Use Open WhatsApp below if this one can't wait that long.`,
        pointToSettings: false,
        offerPopup: true,
      };
    }
    if (limit === "interval" && typeof retryAfterSeconds === "number") {
      return {
        text: `${serverDetail} It'll clear on its own in about ${describeWait(
          retryAfterSeconds,
        )} — no need to open WhatsApp for that.`,
        pointToSettings: false,
        offerPopup: true,
      };
    }
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

/** Turns the server's `retry_after_seconds` into words, for both 429s. Never
 *  rounds up to a bigger unit that overstates the wait, and never invents a
 *  number the server didn't give — the caller only reaches here once it has
 *  a real integer in hand. */
function describeWait(seconds: number): string {
  if (seconds < 60) return `${seconds} second${seconds === 1 ? "" : "s"}`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.round(minutes / 60);
  return `${hours} hour${hours === 1 ? "" : "s"}`;
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

      if (result.status === "pending") {
        // A replay that landed while the first attempt is still in flight.
        // `pending` is *not yet*, not `unknown`'s *we will never know* — the
        // timeline will settle this on its own, so the modal must not close
        // (that would read as sent) and must not offer the popup (a second
        // send now is exactly the double-message this guard exists to
        // prevent).
        setSendError({
          text:
            "This message is still being sent — we'll know shortly. Check the " +
            "activity below in a moment; there's no need to send it again.",
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
        setSendError(
          sendFailureCopy(err.kind, err.session_status, err.message, err.limit, err.retry_after_seconds),
        );
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
    <Dialog
      titleId="wa-modal-title"
      onClose={onClose}
      title={
        <>
          <WhatsappGlyph size={24} />
          WhatsApp {row.full_name}
        </>
      }
    >

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
                    <a href={SETTINGS_WHATSAPP_PATH}>Fix this in Settings → WhatsApp</a>
                    {sendError.offerPopup ? <>, or use Open WhatsApp below.</> : "."}
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
    </Dialog>
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
 * - `pending` is recorded before dispatch and resolves by itself once the
 *   gateway answers — it reads as in-progress, not as something broken or as
 *   something the recruiter must act on. Distinct from `unknown`: `pending`
 *   is *not yet*, `unknown` is *we will never know*, and collapsing them into
 *   one spinner would be as wrong as calling either a failure.
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
  if (item.status === "pending") {
    return `WhatsApp send to ${fullName} by ${item.actor_name} — sending, this will update shortly`;
  }
  if (item.status === "unknown") {
    return `WhatsApp send to ${fullName} by ${item.actor_name} — outcome unknown, check WhatsApp before resending`;
  }
  return `WhatsApp opened for ${fullName} by ${item.actor_name}`;
}

/** How often the timeline refetches while a `pending` row is on screen —
 *  slow enough not to hammer the server, fast enough that "this will update
 *  shortly" in `whatsappActivityLine` stays true. Mirrors the fallback poll
 *  in `whatsapp-panel.tsx`. */
const PENDING_POLL_MS = 5_000;

/** Hard cap on how long the poll runs. The server's own sweep resolves a
 *  stuck `pending` row to `unknown` well inside this window (build note), so
 *  reaching the cap without the poll having already stopped itself is not
 *  expected — but a poll must still have a ceiling rather than running for as
 *  long as the modal happens to stay open. */
const PENDING_POLL_MAX_MS = 3 * 60 * 1000;

/** One row per activity — `opened`, `sent`, `failed`, `pending`, or
 *  `unknown` — reusing `when` from `format.tsx` for the timestamp rather than
 *  writing a second formatter. A refusal (no linked session, no candidate
 *  number) produces no row at all: nothing was attempted, so there is
 *  nothing to log. */
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
  // Guards against a slow refetch (the initial load, or a poll tick)
  // resolving after a newer one and overwriting fresher state — same
  // stale-response protection as the disconnect call in `whatsapp-panel.tsx`.
  const requestId = useRef(0);

  const fetchActivities = useCallback(async () => {
    const myRequestId = ++requestId.current;
    try {
      const list = await getCandidateActivities(row.id);
      if (requestId.current !== myRequestId) return;
      setItems(list);
    } catch (err) {
      if (requestId.current !== myRequestId) return;
      setError(err instanceof ApiError ? err.message : "We could not load activity just now.");
    }
  }, [row.id]);

  useEffect(() => {
    setItems(null);
    setError(null);
    void fetchActivities();
  }, [version, fetchActivities]);

  const whatsappOnly = (items ?? []).filter((item) => item.channel === "whatsapp");
  const anyPending = whatsappOnly.some((item) => item.status === "pending");

  // Bounded poll: the timeline promises a `pending` row "will update
  // shortly" (`whatsappActivityLine` below), and only SSE or a poll can make
  // that true — this file has no SSE subscription, and the backend's own
  // event stream is still mid-flight, so a poll is the honest choice rather
  // than adding a new SSE kind that would race it. Stops the moment nothing
  // is pending, at the hard cap above, and on unmount — the same three stop
  // conditions as the fallback poll in `whatsapp-panel.tsx`.
  useEffect(() => {
    if (!anyPending) return;
    const deadline = Date.now() + PENDING_POLL_MAX_MS;
    const timer = setInterval(() => {
      if (Date.now() >= deadline) {
        clearInterval(timer);
        return;
      }
      void fetchActivities();
    }, PENDING_POLL_MS);
    return () => clearInterval(timer);
  }, [anyPending, fetchActivities]);

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
            // `pending` stays plain-muted: it is not a problem to flag, just
            // a row that has not settled yet — no `role="alert"` either, since
            // nothing here needs the recruiter's attention.
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
