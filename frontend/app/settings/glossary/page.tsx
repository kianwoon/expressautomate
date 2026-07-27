"use client";

import { SettingsShell } from "../settings-shell";
import { Glossary } from "../glossary";

/**
 * The shorthand glossary, on its own route.
 *
 * `Glossary` takes `enabled` and does not fetch until it is true, so that an
 * anonymous visitor never triggers a 401 on the way to the landing page. The
 * shell only renders children once the session is confirmed, which is exactly
 * that condition — hence `enabled` is unconditionally true here.
 */

export default function GlossarySettings() {
  return (
    <SettingsShell heading="Shorthand." active="glossary">
      <Glossary enabled />
    </SettingsShell>
  );
}
