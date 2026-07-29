"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";

import { WA_SESSION_DISCONNECT_PATH, WA_SESSION_PATH } from "../../api";
import { useLive } from "../../events";

/**
 * Linking the recruiter's own WhatsApp — the "WA gateway" (Baileys), not the
 * Meta Cloud API `whatsapp` notifications channel elsewhere in settings.
 *
 * Mirrors `telegram-link-panel.tsx`'s shape (QR display, a poll, expiry,
 * cancel), but two things differ structurally rather than cosmetically:
 *
 * 1. Refetch is SSE-driven, not poll-driven (plan §5: "No polling"). The
 *    server publishes a no-payload `wa_session` nudge over the existing
 *    stream (`../../events`) on every status/QR change; this panel refetches
 *    `GET /api/wa/session` in response. QR strings rotate ~20s, so
 *    nudge → refetch → fresh QR is the actual pairing loop. A slow interval
 *    poll is kept ONLY as a fallback for when the SSE stream itself is down —
 *    see `FALLBACK_POLL_MS` below.
 * 2. The status vocabulary is the database's CHECK constraint, not invented
 *    here: `pairing | connected | reconnecting | disconnected | logged_out`,
 *    plus the API-only `gateway_unreachable`. `pairing` covers both "just
 *    started" and "QR outstanding" — `qr` being non-null is what tells them
 *    apart, there is no separate "qr_required" status.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

/* Only reached when the SSE stream itself is down (see `useLive` / offline
   handling in events.ts) — the nudge is the primary mechanism, so this can be
   slow. Fast enough that an outage does not strand the panel for minutes;
   slow enough that it is not a second poll in disguise. */
const FALLBACK_POLL_MS = 15_000;

/* Same reasoning as the Telegram panel: the encoder option and the <img>'s
   width/height must agree, or the browser box and the image data drift apart. */
const QR_SIZE = 220;

type ApiStatus = "pairing" | "connected" | "reconnecting" | "disconnected" | "logged_out";

type SessionResponse = {
  status: ApiStatus;
  qr: string | null;
  expires_at: string | null;
  phone_number: string | null;
  connected_at: string | null;
  last_checked_at: string | null;
};

type Panel =
  | { status: "loading" }
  | { status: "gateway_unreachable" }
  | { status: "disconnected" }
  | { status: "pairing_starting" }
  | { status: "pairing_qr"; qr: string; qrDataUrl: string | null; expiresAt: number | null }
  | { status: "expired" }
  | { status: "reconnecting" }
  | { status: "logged_out" }
  | { status: "connected"; phoneNumber: string | null; connectedAt: string | null }
  | { status: "request_error"; message: string };

function parseExpiry(iso: string | null): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
}

export function WhatsappPanel() {
  const [panel, setPanel] = useState<Panel>({ status: "loading" });
  const [confirmingDisconnect, setConfirmingDisconnect] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);

  // Bumped whenever a fetch is superseded by a newer one (a nudge-triggered
  // refetch landing after Connect/Cancel/Disconnect, or two refetches racing).
  // Without this a slow GET that resolves after the recruiter has already
  // moved on could silently reopen a panel they backed out of.
  const requestId = useRef(0);

  const applySession = useCallback(async (body: SessionResponse, myRequestId: number) => {
    if (body.status === "pairing" && body.qr) {
      // Never render a QR past its expiry, whether this refetch came from the
      // nudge or from the fallback poll.
      const expiresAt = parseExpiry(body.expires_at);
      if (expiresAt !== null && Date.now() >= expiresAt) {
        if (requestId.current === myRequestId) setPanel({ status: "expired" });
        return;
      }
      let qrDataUrl: string | null = null;
      try {
        qrDataUrl = await QRCode.toDataURL(body.qr, { width: QR_SIZE, margin: 1 });
      } catch {
        // A missing QR image is a degraded panel, not a broken one.
        qrDataUrl = null;
      }
      if (requestId.current !== myRequestId) return;
      setPanel({ status: "pairing_qr", qr: body.qr, qrDataUrl, expiresAt });
      return;
    }
    if (requestId.current !== myRequestId) return;
    if (body.status === "pairing") {
      // Pairing started but no QR has arrived yet — distinct from "QR is on
      // screen", not from a separate status.
      setPanel({ status: "pairing_starting" });
    } else if (body.status === "connected") {
      setPanel({ status: "connected", phoneNumber: body.phone_number, connectedAt: body.connected_at });
    } else if (body.status === "reconnecting") {
      setPanel({ status: "reconnecting" });
    } else if (body.status === "logged_out") {
      setPanel({ status: "logged_out" });
    } else {
      setPanel({ status: "disconnected" });
    }
  }, []);

  const load = useCallback(async () => {
    const myRequestId = requestId.current;
    try {
      const res = await fetch(WA_SESSION_PATH, {
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (requestId.current !== myRequestId) return;
      if (!res.ok) {
        // Not "you are disconnected" — we genuinely do not know, and saying
        // otherwise would invite a re-pair that was never needed.
        setPanel({ status: "gateway_unreachable" });
        return;
      }
      const body = (await res.json()) as SessionResponse;
      if ((body.status as string) === "gateway_unreachable") {
        if (requestId.current === myRequestId) setPanel({ status: "gateway_unreachable" });
        return;
      }
      await applySession(body, myRequestId);
    } catch {
      if (requestId.current !== myRequestId) return;
      setPanel({ status: "gateway_unreachable" });
    }
  }, [applySession]);

  useEffect(() => {
    void load();
  }, [load]);

  // Primary refetch mechanism: the server nudges over the shared SSE stream
  // (no payload, see events.ts) on every status/QR change. This is what
  // actually rotates the QR every ~20s, not the fallback poll below.
  useLive(
    useCallback(
      (nudge) => {
        if (nudge === "wa_session" || nudge === "open") void load();
      },
      [load],
    ),
  );

  const connect = useCallback(async () => {
    const myRequestId = ++requestId.current;
    setPanel({ status: "pairing_starting" });
    try {
      const res = await fetch(WA_SESSION_PATH, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (requestId.current !== myRequestId) return;
      if (!res.ok) {
        setPanel({ status: "request_error", message: "We could not start a WhatsApp link just now." });
        return;
      }
      const body = (await res.json()) as SessionResponse;
      await applySession(body, myRequestId);
    } catch {
      if (requestId.current !== myRequestId) return;
      setPanel({ status: "request_error", message: "We could not reach the server." });
    }
  }, [applySession]);

  const cancel = useCallback(() => {
    requestId.current++;
    setPanel({ status: "disconnected" });
  }, []);

  const disconnect = useCallback(async () => {
    setDisconnecting(true);
    const myRequestId = ++requestId.current;
    try {
      const res = await fetch(WA_SESSION_DISCONNECT_PATH, {
        method: "POST",
        credentials: "include",
        headers: { Accept: "application/json" },
      });
      if (requestId.current !== myRequestId) return;
      if (!res.ok) {
        setPanel({ status: "request_error", message: "We could not disconnect just now." });
        return;
      }
      setPanel({ status: "disconnected" });
    } catch {
      if (requestId.current !== myRequestId) return;
      setPanel({ status: "request_error", message: "We could not reach the server." });
    } finally {
      setDisconnecting(false);
      setConfirmingDisconnect(false);
    }
  }, []);

  // Fallback poll for when the SSE stream is down — kept slow, and stops the
  // moment there is nothing left to wait for (a status change, Cancel,
  // unmount), same stop conditions as before.
  const pollExpiresAt = panel.status === "pairing_qr" ? panel.expiresAt : null;
  const shouldPoll = panel.status === "pairing_starting" || panel.status === "pairing_qr";
  useEffect(() => {
    if (!shouldPoll) return;
    const timer = setInterval(() => {
      if (pollExpiresAt !== null && Date.now() >= pollExpiresAt) {
        setPanel({ status: "expired" });
        return;
      }
      void load();
    }, FALLBACK_POLL_MS);
    return () => clearInterval(timer);
  }, [shouldPoll, pollExpiresAt, load]);

  if (panel.status === "loading") {
    return (
      <div className="nt-card">
        <div className="nt-card-head">
          <span className="nt-card-title">WhatsApp</span>
        </div>
        <p className="nt-note">Checking your WhatsApp link.</p>
      </div>
    );
  }

  if (panel.status === "gateway_unreachable") {
    return (
      <div className="nt-card">
        <div className="nt-card-head">
          <span className="nt-card-title">WhatsApp</span>
        </div>
        <p className="nt-note">
          We cannot tell right now whether your WhatsApp is linked. This is not the same as being
          disconnected — reload in a moment before pairing again.
        </p>
        <div className="nt-card-foot">
          <button className="btn btn-secondary" onClick={() => void load()}>
            Try again
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="nt-card">
      <div className="nt-card-head">
        <span className="nt-card-title">WhatsApp</span>
      </div>

      {panel.status === "disconnected" ? (
        <>
          <p className="nt-note">
            Connecting links your own WhatsApp account to this recruiter profile. Messages to
            candidates will be sent from your own WhatsApp number, not a shared or automated one. We
            never see or store your WhatsApp password.
          </p>
          <div className="nt-card-foot">
            <button className="btn btn-primary" onClick={() => void connect()}>
              Connect WhatsApp
            </button>
          </div>
        </>
      ) : panel.status === "pairing_starting" ? (
        <>
          <p className="nt-note">Starting the link.</p>
          <div className="nt-card-foot">
            <button className="btn btn-secondary" onClick={cancel}>
              Cancel
            </button>
          </div>
        </>
      ) : panel.status === "pairing_qr" ? (
        <>
          <p className="nt-note">
            On your phone: open WhatsApp, go to Settings → Linked devices → Link a device, and scan
            this code.
          </p>
          {panel.qrDataUrl ? (
            <img className="nt-qr" src={panel.qrDataUrl} alt="" width={QR_SIZE} height={QR_SIZE} />
          ) : null}
          {panel.expiresAt !== null ? (
            <p className="nt-note">
              This code expires at {new Date(panel.expiresAt).toLocaleTimeString()}.
            </p>
          ) : null}
          <div className="nt-card-foot">
            <button className="btn btn-secondary" onClick={cancel}>
              Cancel
            </button>
          </div>
        </>
      ) : panel.status === "expired" ? (
        <>
          <p className="nt-note">That code expired. Start again when you are ready.</p>
          <div className="nt-card-foot">
            <button className="btn btn-primary" onClick={() => void connect()}>
              Connect WhatsApp
            </button>
          </div>
        </>
      ) : panel.status === "reconnecting" ? (
        <>
          <p className="nt-note">
            Reconnecting… WhatsApp is already linked; we are just re-establishing the connection after
            a restart. This usually takes under a minute and needs nothing from you.
          </p>
        </>
      ) : panel.status === "logged_out" ? (
        <>
          <p className="nt-note">
            WhatsApp ended the link on this device. This can happen if the device was removed from
            Linked devices on the phone itself. Link again to keep sending from your WhatsApp number.
          </p>
          <div className="nt-card-foot">
            <button className="btn btn-primary" onClick={() => void connect()}>
              Connect WhatsApp
            </button>
          </div>
        </>
      ) : panel.status === "request_error" ? (
        <>
          <p className="nt-error">{panel.message}</p>
          <div className="nt-card-foot">
            <button className="btn btn-secondary" onClick={() => void connect()}>
              Try again
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="nt-note">
            WhatsApp accepted the link{panel.phoneNumber ? ` for ${panel.phoneNumber}` : ""}. Messages
            to candidates will go out from this number.
          </p>
          {panel.connectedAt ? (
            <p className="nt-note">Connected {new Date(panel.connectedAt).toLocaleString()}.</p>
          ) : null}
          {confirmingDisconnect ? (
            <>
              <p className="nt-note">
                Disconnecting stops candidate messages from going out until you link again. Continue?
              </p>
              <div className="nt-card-foot">
                <button
                  className="btn btn-secondary"
                  onClick={() => void disconnect()}
                  disabled={disconnecting}
                >
                  {disconnecting ? "Disconnecting…" : "Yes, disconnect"}
                </button>
                <button
                  className="btn btn-secondary"
                  onClick={() => setConfirmingDisconnect(false)}
                  disabled={disconnecting}
                >
                  Keep connected
                </button>
              </div>
            </>
          ) : (
            <div className="nt-card-foot">
              <button className="btn btn-secondary" onClick={() => setConfirmingDisconnect(true)}>
                Disconnect
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
