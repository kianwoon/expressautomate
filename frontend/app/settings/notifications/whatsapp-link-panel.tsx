"use client";

import { useState } from "react";

import { SETTINGS_WHATSAPP_PATH } from "../../api";
import type { Destination } from "./notifications-data";

/**
 * Turning an already-paired WhatsApp device into a notification destination.
 *
 * Mirrors `telegram-link-panel.tsx`'s shape (a card under "Add a
 * destination", hidden once linked) but is far simpler: there is no QR and no
 * polling here, because the pairing itself happened on the WhatsApp tab
 * (`../whatsapp/whatsapp-panel.tsx`) — this panel only offers to turn that
 * already-connected device into a destination. Where Telegram tracks a
 * baseline of destination ids to notice one appearing mid-flow (its QR panel
 * stays open across the change), this panel has no such window: it simply
 * hides once a `whatsapp_linked` destination is present in the current list.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export function WhatsAppLinkPanel({
  linked,
  linkedNumber,
  destinations,
  onAdd,
}: {
  linked: boolean;
  linkedNumber: string | null;
  destinations: Destination[];
  onAdd: () => Promise<string | null>;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // The destination card above already covers this once it exists — nothing
  // left for this panel to offer.
  const alreadyAdded = destinations.some((d) => d.channel === "whatsapp_linked");
  if (alreadyAdded) return null;

  async function add() {
    if (busy) return;
    setBusy(true);
    setError(null);
    setError(await onAdd());
    setBusy(false);
  }

  if (!linked) {
    return (
      <div className="nt-card nt-card-muted">
        <div className="nt-card-head">
          <span className="nt-card-title">WhatsApp</span>
          <span className="nt-badge nt-badge-off">Not linked</span>
        </div>
        <p className="nt-note">
          Link your own WhatsApp in <a href={SETTINGS_WHATSAPP_PATH}>Settings -&gt; WhatsApp</a>{" "}
          first, then come back here to receive notifications on it.
        </p>
      </div>
    );
  }

  return (
    <div className="nt-card">
      <div className="nt-card-head">
        <span className="nt-card-title">WhatsApp</span>
      </div>
      <p className="nt-note">
        Send notifications to your linked number{linkedNumber ? ` (${linkedNumber})` : ""}.
      </p>
      {error ? <p className="nt-error">{error}</p> : null}
      <div className="nt-card-foot">
        <button className="btn btn-primary" disabled={busy} onClick={() => void add()}>
          Add
        </button>
      </div>
    </div>
  );
}
