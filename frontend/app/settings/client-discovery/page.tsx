"use client";

import { SettingsShell } from "../settings-shell";
import { DiscoveryPanel } from "./discovery-panel";

/**
 * Where a recruiter scans their own mailbox for the companies they already
 * talk to — headers only, never message bodies.
 */

export default function ClientDiscoverySettings() {
  return (
    <SettingsShell heading="Client discovery." active="discovery">
      <DiscoveryPanel />
    </SettingsShell>
  );
}
