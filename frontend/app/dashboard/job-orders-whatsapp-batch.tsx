"use client";

import { useEffect, useRef, useState } from "react";

import { SETTINGS_WHATSAPP_PATH } from "../api";
import {
  ApiError,
  getWhatsappBatchDraft,
  sendCandidateWhatsapp,
  WhatsappSendError,
} from "./candidates";
import type { WhatsappRecipient } from "./candidates/candidate-whatsapp";
import { WhatsappGlyph } from "./candidates/candidate-whatsapp";
import { Dialog } from "./dialog";

/**
 * The batch WhatsApp modal: one message to several shortlisted candidates.
 *
 * The recruiter edits ONE shared body and sends it to every selected
 * candidate. Each candidate is greeted by their own name — the greeting line
 * is prepended server-side (`prepend_greeting`), so the name on the wire
 * comes from the candidate's row, never from this form.
 *
 * Sending is deliberately sequential, one candidate at a time, with a live
 * progress list — not a parallel burst. The server enforces a per-session
 * spacing floor (`WA_SEND_MIN_INTERVAL_SECONDS`) to keep the recruiter's own
 * number off WhatsApp's spam radar, and the daily cap (`WA_SEND_DAILY_LIMIT`)
 * applies to every send the same way it does to a single one. So the modal
 * sends, reads each response, waits out the spacing before the next send, and
 * reports each candidate's outcome as it goes.
 */

type SendPhase =
  | "idle"
  | "composing"
  | "sending"
  | "done";

type CandidateOutcome = {
  id: string;
  status:
    | "queued"
    | "sending"
    | "sent"
    | "failed"
    | "skipped_no_number"
    | "not_attempted";
  detail: string | null;
};

/** A wait long enough that the recruiter should see it, in the same words the
 *  single-candidate modal uses. */
function describeWait(seconds: number): string {
  if (seconds < 60) return `${seconds} second${seconds === 1 ? "" : "s"}`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.round(minutes / 60);
  return `${hours} hour${hours === 1 ? "" : "s"}`;
}

/** A fresh id per composed message, not per click — the batch mints one per
 *  candidate, so retrying a candidate whose first response was slow cannot
 *  double-send to them. */
function newClientRequestId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `wa-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function WhatsappBatchModal({
  candidates,
  jobTitle,
  onClose,
}: {
  /** The selected shortlist matches, in rank order. Every one carries the
   *  contact facts already fetched for the card — no extra requests here. */
  candidates: WhatsappRecipient[];
  /** The job order this shortlist is for; names the opportunity in the
   *  shared body. */
  jobTitle: string | null;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<{ status: "loading" } | { status: "ready"; message: string } | { status: "error"; message: string }>({
    status: "loading",
  });
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<SendPhase>("idle");
  const [outcomes, setOutcomes] = useState<CandidateOutcome[]>([]);
  const [sendError, setSendError] = useState<{ text: string; pointToSettings: boolean } | null>(null);
  // The candidate being sent right now, or null when idle. Also the anchor
  // for the progress list: "sending X of N".
  const [sendingIndex, setSendingIndex] = useState<number | null>(null);
  const cancelledRef = useRef(false);

  // A client_request_id per candidate, minted once the batch starts — a retry
  // of the same candidate reuses the same key, so the server's unique index
  // turns a timeout-then-retry into a no-op instead of a second message.
  const keysRef = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const body = await getWhatsappBatchDraft(jobTitle);
        if (cancelled) return;
        setDraft({ status: "ready", message: body.message });
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
  }, [jobTitle]);

  useEffect(() => () => {
    cancelledRef.current = true;
  }, []);

  function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function sendOne(index: number): Promise<void> {
    const candidate = candidates[index];
    if (!candidate) return;

    setSendingIndex(index);
    setOutcomes((prev) =>
      prev.map((o) => (o.id === candidate.id ? { ...o, status: "sending", detail: null } : o)),
    );

    if (!candidate.phone_e164) {
      setOutcomes((prev) =>
        prev.map((o) =>
          o.id === candidate.id
            ? { ...o, status: "skipped_no_number", detail: "No WhatsApp-reachable number on file." }
            : o,
        ),
      );
      setSendingIndex(null);
      return;
    }

    const key = keysRef.current.get(candidate.id) ?? newClientRequestId();
    keysRef.current.set(candidate.id, key);

    try {
      const result = await sendCandidateWhatsapp(candidate.id, text, key, {
        prependGreeting: true,
      });
      if (result.status === "sent") {
        setOutcomes((prev) =>
          prev.map((o) => (o.id === candidate.id ? { ...o, status: "sent", detail: null } : o)),
        );
        return;
      }
      if (result.status === "unknown") {
        setOutcomes((prev) =>
          prev.map((o) =>
            o.id === candidate.id
              ? {
                  ...o,
                  status: "failed",
                  detail:
                    "Sent to WhatsApp but no confirmation came back — check WhatsApp before resending.",
                }
              : o,
          ),
        );
        return;
      }
      if (result.status === "pending") {
        setOutcomes((prev) =>
          prev.map((o) =>
            o.id === candidate.id
              ? {
                  ...o,
                  status: "failed",
                  detail: "This message is still sending — the activity log will settle it.",
                }
              : o,
          ),
        );
        return;
      }
    } catch (err) {
      if (err instanceof WhatsappSendError && err.kind === "rate_limited") {
        if (err.limit === "daily") {
          // The daily cap is a batch-level stop: every remaining candidate
          // would hit the same wall, so don't burn N requests discovering it.
          setOutcomes((prev) =>
            prev.map((o) =>
              o.id === candidate.id
                ? { ...o, status: "failed", detail: err.message }
                : o.status === "queued"
                  ? { ...o, status: "not_attempted", detail: "Daily send limit reached." }
                  : o,
            ),
          );
          setSendError({
            text: `${err.message} Open WhatsApp instead for anyone this can't wait for.`,
            pointToSettings: false,
          });
          setSendingIndex(null);
          return;
        }
        if (err.limit === "interval" && typeof err.retry_after_seconds === "number") {
          // The spacing floor — wait it out, then retry the SAME candidate
          // with the SAME key. Nothing was sent, so the retry is safe.
          setOutcomes((prev) =>
            prev.map((o) =>
              o.id === candidate.id
                ? {
                    ...o,
                    status: "sending",
                    detail: `Waiting ${describeWait(err.retry_after_seconds ?? 0)} for the anti-spam pause…`,
                  }
                : o,
            ),
          );
          await sleep((err.retry_after_seconds ?? 30) * 1000);
          if (cancelledRef.current) return;
          return sendOne(index);
        }
      }
      if (err instanceof WhatsappSendError && err.kind === "risk_not_acknowledged") {
        setOutcomes((prev) =>
          prev.map((o) =>
            o.id === candidate.id
              ? { ...o, status: "failed", detail: err.message }
              : o.status === "queued"
                ? { ...o, status: "not_attempted", detail: "Risk notice not acknowledged." }
                : o,
          ),
        );
        setSendError({
          text: `${err.message} Acknowledge the risk notice in Settings → WhatsApp before sending.`,
          pointToSettings: true,
        });
        setSendingIndex(null);
        return;
      }
      // Anything else is a per-candidate failure — report it and move on,
      // because one broken send must not sink the rest of the batch.
      const detail = err instanceof ApiError ? err.message : "We could not send that just now.";
      setOutcomes((prev) =>
        prev.map((o) => (o.id === candidate.id ? { ...o, status: "failed", detail } : o)),
      );
    } finally {
      setSendingIndex((prev) => (prev === index ? null : prev));
    }
  }

  async function sendBatch() {
    if (phase === "sending") return;
    setPhase("sending");
    setSendError(null);
    setOutcomes(candidates.map((c) => ({ id: c.id, status: "queued", detail: null })));
    cancelledRef.current = false;

    for (let i = 0; i < candidates.length; i += 1) {
      if (cancelledRef.current) break;
      await sendOne(i);
      if (cancelledRef.current) break;
    }

    setPhase("done");
    setSendingIndex(null);
  }

  const sentCount = outcomes.filter((o) => o.status === "sent").length;
  const failedCount = outcomes.filter(
    (o) => o.status === "failed" || o.status === "not_attempted" || o.status === "skipped_no_number",
  ).length;
  const busy = phase === "sending";

  return (
    <Dialog
      titleId="wa-batch-modal-title"
      onClose={busy ? () => {} : onClose}
      title={
        <>
          <WhatsappGlyph size={24} />
          WhatsApp {candidates.length} candidates
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
          <p className="body muted">
            One message, sent to each candidate with their own name on the greeting line. The
            server spaces sends to protect your WhatsApp number from anti-spam flags.
          </p>

          <label className="wa-field">
            <span className="row-k">Message</span>
            <textarea
              className="jo-search"
              style={{ minHeight: 160 }}
              value={text}
              rows={7}
              disabled={busy}
              onChange={(e) => setText(e.target.value)}
            />
          </label>

          <ul className="src-batch-list">
            {candidates.map((candidate, index) => {
              const outcome = outcomes.find((o) => o.id === candidate.id);
              const status = outcome?.status ?? "queued";
              const isCurrent = sendingIndex === index;
              return (
                <li
                  key={candidate.id}
                  className={`src-batch-row${isCurrent ? " src-batch-current" : ""}`}
                  data-status={status}
                >
                  <span className="src-batch-name">{candidate.full_name}</span>
                  <span className="src-batch-status">
                    {status === "queued" && (isCurrent ? "Sending…" : "Waiting…")}
                    {status === "sending" && (outcome?.detail ?? "Sending…")}
                    {status === "sent" && "Sent"}
                    {status === "failed" && (outcome?.detail ?? "Failed")}
                    {status === "skipped_no_number" && (outcome?.detail ?? "No number")}
                    {status === "not_attempted" && (outcome?.detail ?? "Not attempted")}
                  </span>
                </li>
              );
            })}
          </ul>

          {sendError && (
            <p className="body src-error" role="alert">
              {sendError.text}
              {sendError.pointToSettings && (
                <>
                  {" "}
                  <a href={SETTINGS_WHATSAPP_PATH}>Fix this in Settings → WhatsApp</a>.
                </>
              )}
            </p>
          )}

          <div className="wa-actions">
            {phase === "idle" && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void sendBatch()}
                disabled={!text.trim() || candidates.length === 0}
              >
                Send to {candidates.length} candidates
              </button>
            )}
            {phase === "sending" && (
              <span className="body muted">
                Sending… {sentCount} of {candidates.length} sent
              </span>
            )}
            {phase === "done" && (
              <span className="body muted">
                {sentCount} sent, {failedCount} not sent.
              </span>
            )}
            {!busy && (
              <button type="button" className="btn btn-secondary" onClick={onClose}>
                {phase === "done" ? "Close" : "Cancel"}
              </button>
            )}
          </div>
        </>
      )}
    </Dialog>
  );
}
