"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import QRCode from "qrcode";

/**
 * Linking Telegram.
 *
 * The recruiter is on a laptop and Telegram is on their phone, so the QR is
 * the common path and the link is the fallback for whoever runs Telegram
 * Desktop.
 *
 * Nothing tells this page when Start is pressed — the update goes to our
 * webhook, not to the browser — so the panel polls while it is open and stops
 * the moment a destination appears. It polls ONLY while open: a settings page
 * left in a background tab issues no requests.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

/* Fast enough that pressing Start feels instant, slow enough that a
   fifteen-minute window is a few hundred requests rather than thousands. */
const POLL_MS = 3000;

type Panel =
  | { status: "closed" }
  | { status: "opening" }
  | { status: "open"; url: string; expiresAt: number }
  | { status: "expired" }
  | { status: "failed"; message: string };

export function TelegramLinkPanel({
  available,
  onRequestLink,
  onPoll,
  linkedCount,
}: {
  available: boolean;
  onRequestLink: () => Promise<{ url: string; expiresInMinutes: number } | { error: string }>;
  onPoll: () => void;
  linkedCount: number;
}) {
  const [panel, setPanel] = useState<Panel>({ status: "closed" });
  const [qr, setQr] = useState<string | null>(null);
  // The count at the moment the panel opened. Polling watches for it to rise
  // rather than for a specific id, because the id is created server-side by
  // the webhook and this page never learns it any other way.
  const baseline = useRef(linkedCount);
  // The poll callback lives in a ref, not in the effect's dependencies. The
  // parent passes an inline arrow, so its identity changes on every render; a
  // dependency on it would clear and re-create the interval each time and the
  // three-second timer would keep resetting before it ever fired.
  const poll = useRef(onPoll);
  useEffect(() => {
    poll.current = onPoll;
  }, [onPoll]);

  const open = useCallback(async () => {
    setPanel({ status: "opening" });
    setQr(null);
    baseline.current = linkedCount;
    const result = await onRequestLink();
    if ("error" in result) {
      setPanel({ status: "failed", message: result.error });
      return;
    }
    setPanel({
      status: "open",
      url: result.url,
      expiresAt: Date.now() + result.expiresInMinutes * 60_000,
    });
    try {
      setQr(await QRCode.toDataURL(result.url, { width: 220, margin: 1 }));
    } catch {
      // A missing QR is a degraded panel, not a broken one — the link below it
      // still works, so this failure is deliberately silent on screen.
      setQr(null);
    }
  }, [linkedCount, onRequestLink]);

  // A new destination appeared while we were waiting: that is the link
  // completing. Closing here is what makes the flow feel finished.
  useEffect(() => {
    if (panel.status === "open" && linkedCount > baseline.current) {
      setPanel({ status: "closed" });
      setQr(null);
    }
  }, [linkedCount, panel]);

  // Depends on the panel's status and expiry only — see the ref above for why
  // the callback is deliberately absent from this list.
  const openUntil = panel.status === "open" ? panel.expiresAt : null;
  useEffect(() => {
    if (openUntil === null) return;
    const timer = setInterval(() => {
      if (Date.now() >= openUntil) {
        // Leaving a dead QR on screen invites someone to scan it and get
        // nothing, with no explanation.
        setPanel({ status: "expired" });
        setQr(null);
        return;
      }
      poll.current();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [openUntil]);

  if (!available) {
    return (
      <div className="nt-card nt-card-muted">
        <div className="nt-card-head">
          <span className="nt-card-title">Telegram</span>
          <span className="nt-badge nt-badge-off">Not configured</span>
        </div>
        <p className="nt-note">
          Telegram is not set up on this deployment yet.
        </p>
      </div>
    );
  }

  return (
    <div className="nt-card">
      <div className="nt-card-head">
        <span className="nt-card-title">Telegram</span>
      </div>

      {panel.status === "closed" ? (
        <>
          <p className="nt-note">
            Open the link on the phone you want messages on, and press Start.
          </p>
          <div className="nt-card-foot">
            <button className="btn btn-primary" onClick={() => void open()}>
              Link Telegram
            </button>
          </div>
        </>
      ) : panel.status === "opening" ? (
        <p className="nt-note">Creating a link.</p>
      ) : panel.status === "failed" ? (
        <>
          <p className="nt-error">{panel.message}</p>
          <div className="nt-card-foot">
            <button className="btn btn-secondary" onClick={() => void open()}>
              Try again
            </button>
          </div>
        </>
      ) : panel.status === "expired" ? (
        <>
          <p className="nt-note">That link expired. Create a new one when you are ready.</p>
          <div className="nt-card-foot">
            <button className="btn btn-primary" onClick={() => void open()}>
              New link
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="nt-note">
            Scan this with your phone, or open the link if Telegram is on this machine. Then press
            Start. This page will notice by itself.
          </p>
          {qr ? <img className="nt-qr" src={qr} alt="" width={220} height={220} /> : null}
          <p className="nt-note">
            <a href={panel.url} target="_blank" rel="noreferrer noopener">
              {panel.url}
            </a>
          </p>
          <div className="nt-card-foot">
            <button
              className="btn btn-secondary"
              onClick={() => {
                setPanel({ status: "closed" });
                setQr(null);
              }}
            >
              Cancel
            </button>
          </div>
        </>
      )}
    </div>
  );
}
