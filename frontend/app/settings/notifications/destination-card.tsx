"use client";

import { useState } from "react";

import { eventLabel, type Destination, type NotificationEvent } from "./notifications-data";

/**
 * One linked destination, and what it is subscribed to.
 *
 * A card rather than a row in a matrix because a destination carries state —
 * verified, disabled, which account it belongs to — that a checkbox grid has
 * nowhere to put. With two event kinds a grid would also be mostly empty
 * chrome.
 *
 * Unlinking asks first. It is reversible, but silently dropping a destination
 * someone relies on is worse than one extra click.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

const CHANNEL_LABELS: Record<string, string> = { telegram: "Telegram", whatsapp: "WhatsApp" };

export function DestinationCard({
  destination,
  events,
  onSetEvents,
  onUnlink,
}: {
  destination: Destination;
  events: NotificationEvent[];
  onSetEvents: (kinds: string[]) => Promise<string | null>;
  onUnlink: () => Promise<string | null>;
}) {
  // Separate from any load error: this means we showed the setting and could
  // not change it, which needs different words and must not blank the card.
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  async function toggle(kind: string, on: boolean) {
    if (busy) return;
    setBusy(true);
    setError(null);
    // The endpoint REPLACES this destination's subscriptions, so the full set
    // goes every time. Sending only the changed kind would silently untick
    // everything else.
    const next = on
      ? [...destination.event_kinds, kind]
      : destination.event_kinds.filter((k) => k !== kind);
    setError(await onSetEvents(next));
    setBusy(false);
  }

  async function confirmUnlink() {
    setBusy(true);
    setError(null);
    setError(await onUnlink());
    setBusy(false);
    setConfirming(false);
  }

  return (
    <div className="nt-card">
      <div className="nt-card-head">
        <span className="nt-card-title">
          {CHANNEL_LABELS[destination.channel] ?? destination.channel}
        </span>
        {destination.disabled ? (
          <span className="nt-badge nt-badge-off">Disabled</span>
        ) : destination.verified ? (
          <span className="nt-badge">Verified</span>
        ) : (
          <span className="nt-badge nt-badge-off">Not verified</span>
        )}
      </div>

      {destination.disabled ? (
        <p className="nt-note">
          We stopped sending to this destination after repeated failures — most often the bot was
          blocked. Unlink it and link again to start over.
        </p>
      ) : null}

      {events.length === 0 ? (
        // No event kinds to offer at all is a different situation from none
        // being ticked (handled below) — a labelled, empty fieldset reads as
        // broken, so this explains rather than rendering a blank box.
        <p className="nt-note">There is nothing to subscribe to yet.</p>
      ) : (
        <fieldset className="nt-events" disabled={busy}>
          <legend className="nt-events-legend">Tell me when</legend>
          {events.map((e) => (
            <label className="nt-event" key={e.kind}>
              <input
                type="checkbox"
                checked={destination.event_kinds.includes(e.kind)}
                onChange={(ev) => void toggle(e.kind, ev.target.checked)}
              />
              <span>{eventLabel(e.kind)}</span>
            </label>
          ))}
        </fieldset>
      )}

      {events.length > 0 && destination.event_kinds.length === 0 ? (
        <p className="nt-note">
          Nothing is ticked, so this destination will not receive anything.
        </p>
      ) : null}

      {error ? <p className="nt-error">{error}</p> : null}

      <div className="nt-card-foot">
        {confirming ? (
          <>
            <span className="nt-note">Unlink this destination?</span>
            <button className="btn btn-secondary" disabled={busy} onClick={() => void confirmUnlink()}>
              Yes, unlink
            </button>
            <button className="btn btn-secondary" disabled={busy} onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </>
        ) : (
          <button className="btn btn-secondary" disabled={busy} onClick={() => setConfirming(true)}>
            Unlink
          </button>
        )}
      </div>
    </div>
  );
}
