"use client";

import { SettingsShell } from "../settings-shell";
import { DestinationCard } from "./destination-card";
import { useNotifications } from "./notifications-data";
import { TelegramLinkPanel } from "./telegram-link-panel";

/**
 * Where job orders get sent.
 *
 * WhatsApp is rendered whether or not it is configured, greyed with a reason
 * when it is not: the API reports `channels.whatsapp`, and hiding the block
 * would leave nothing on screen to say the channel is coming. When the flag
 * flips true the block becomes live with no change here.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export default function NotificationsSettings() {
  const { state, setEvents, unlink, requestTelegramLink, reload } = useNotifications();

  return (
    <SettingsShell heading="Notifications." active="notifications">
      <p className="lede" style={{ marginTop: 18, maxWidth: "62ch" }}>
        Job orders arrive in the evening, and nobody opens a dashboard in the evening. Link a
        destination and we will tell you as they land.
      </p>

      {state.status === "loading" ? (
        <p className="nt-note">Reading your settings.</p>
      ) : state.status === "unreadable" ? (
        <p className="nt-error">{state.message}</p>
      ) : (
        <>
          {state.settings.destinations.length === 0 ? (
            <p className="nt-note">
              Nothing is linked yet, so nothing is being sent.
            </p>
          ) : (
            state.settings.destinations.map((d) => (
              <DestinationCard
                key={d.id}
                destination={d}
                events={state.settings.events}
                onSetEvents={(kinds) => setEvents(d.id, kinds)}
                onUnlink={() => unlink(d.id)}
              />
            ))
          )}

          <h2 className="eyebrow" style={{ marginTop: 34 }}>
            Add a destination
          </h2>

          <TelegramLinkPanel
            available={state.settings.channels.telegram}
            onRequestLink={requestTelegramLink}
            onPoll={() => void reload()}
            linkedCount={state.settings.destinations.length}
          />

          <div className={`nt-card ${!state.settings.channels.whatsapp ? "nt-card-muted" : ""}`}>
            <div className="nt-card-head">
              <span className="nt-card-title">WhatsApp</span>
              <span className={`nt-badge ${!state.settings.channels.whatsapp ? "nt-badge-off" : ""}`}>
                {state.settings.channels.whatsapp ? "Available" : "Not yet available"}
              </span>
            </div>
            {!state.settings.channels.whatsapp && (
              <p className="nt-note">
                WhatsApp needs a verified business account, which is still being approved. Telegram
                works today.
              </p>
            )}
          </div>
        </>
      )}
    </SettingsShell>
  );
}
