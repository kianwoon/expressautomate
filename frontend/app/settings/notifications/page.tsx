"use client";

import { SettingsShell } from "../settings-shell";
import { DestinationCard } from "./destination-card";
import { useNotifications } from "./notifications-data";
import { TelegramLinkPanel } from "./telegram-link-panel";
import { WhatsAppLinkPanel } from "./whatsapp-link-panel";

/**
 * Where job orders get sent.
 *
 * Two, unrelated things are both called "WhatsApp" here and must not be
 * confused: `WhatsAppLinkPanel` turns the recruiter's own paired device (see
 * Settings -> WhatsApp) into a destination, live today. The card below it,
 * "WhatsApp Business", is the still-unshipped Meta Cloud API channel — it is
 * rendered whether or not it is configured, greyed with a reason when it is
 * not, so hiding it would leave nothing on screen to say that channel is
 * coming. When its flag flips true the block becomes live with no change
 * here. The retitle to "WhatsApp Business" is what keeps a recruiter from
 * reading two cards both called plain "WhatsApp" on one screen.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export default function NotificationsSettings() {
  const { state, setEvents, unlink, requestTelegramLink, addWhatsAppLinkedDestination, reload } =
    useNotifications();

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
          {/* A refresh failure (as opposed to the first load) never replaces
              this branch — see notifications-data.ts. It is a non-destructive
              notice on top of whatever was already on screen, so an outage
              while the Telegram panel is open leaves the panel, its QR and
              its timer untouched. */}
          {state.refreshError ? <p className="nt-error">{state.refreshError}</p> : null}
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
            destinationIds={state.settings.destinations.map((d) => d.id)}
          />

          <WhatsAppLinkPanel
            linked={state.settings.channels.whatsapp_linked}
            linkedNumber={state.settings.whatsapp_linked_number}
            destinations={state.settings.destinations}
            onAdd={addWhatsAppLinkedDestination}
          />

          <div className={`nt-card ${!state.settings.channels.whatsapp ? "nt-card-muted" : ""}`}>
            <div className="nt-card-head">
              <span className="nt-card-title">WhatsApp Business</span>
              <span className={`nt-badge ${!state.settings.channels.whatsapp ? "nt-badge-off" : ""}`}>
                {state.settings.channels.whatsapp ? "Available" : "Not yet available"}
              </span>
            </div>
            {!state.settings.channels.whatsapp && (
              <p className="nt-note">
                WhatsApp Business needs a verified Meta business account, which is still being
                approved. Telegram, and your own linked WhatsApp above, work today.
              </p>
            )}
          </div>
        </>
      )}
    </SettingsShell>
  );
}
