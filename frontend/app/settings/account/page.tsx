"use client";

import { SettingsShell } from "../settings-shell";
import { AccountSetting } from "../account";

/**
 * Where a recruiter sets the name a candidate sees in a WhatsApp message.
 */

export default function AccountSettings() {
  return (
    <SettingsShell heading="Account." active="account">
      <AccountSetting />
    </SettingsShell>
  );
}
