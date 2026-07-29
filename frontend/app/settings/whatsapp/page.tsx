"use client";

import { SettingsShell } from "../settings-shell";
import { WhatsappPanel } from "./whatsapp-panel";

/**
 * Where a recruiter links their own WhatsApp account.
 */

export default function WhatsappSettings() {
  return (
    <SettingsShell heading="WhatsApp." active="whatsapp">
      <WhatsappPanel />
    </SettingsShell>
  );
}
