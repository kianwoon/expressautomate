"use client";

import { IntakePause } from "../dashboard/intake-pause";
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
      {/* The same control as the dashboard header pill, mirrored here because
          this page is where someone goes *looking* for a switch they know
          exists — the pill is the fast path, this is the findable one. */}
      <h2 className="eyebrow" style={{ marginTop: 22 }}>
        Whether we read your inbox at all
      </h2>
      <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
        Pausing stops your mailbox feeding the dashboard — going on leave is the usual reason.
        Email that arrives while intake is paused is never read: resuming starts again from that
        moment, with no catch-up.
      </p>
      <div style={{ marginTop: 16 }}>
        <IntakePause />
      </div>

      <h2 className="eyebrow" style={{ marginTop: 44 }}>
        How far back we read your inbox
      </h2>
      <LookbackSetting />
    </SettingsShell>
  );
}
