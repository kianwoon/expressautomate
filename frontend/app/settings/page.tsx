"use client";

import { SettingsShell } from "./settings-shell";
import { LookbackSetting } from "./lookback";

/**
 * How far back we read the inbox.
 *
 * This stays at `/settings` rather than moving to `/settings/inbox`: existing
 * links keep working, and a static export cannot redirect without rendering
 * something first. The sub-nav therefore labels it "Inbox" while the URL says
 * `/settings` — a deliberate asymmetry, taken over shipping a redirect that
 * exists only for symmetry.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export default function Settings() {
  return (
    <SettingsShell heading="Settings." active="inbox">
      <h2 className="eyebrow" style={{ marginTop: 22 }}>
        How far back we read your inbox
      </h2>
      <LookbackSetting />
    </SettingsShell>
  );
}
