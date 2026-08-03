import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WhatsappPanel } from "./whatsapp-panel";

/**
 * Pinned here: the connected sentence renders whatever `phone_number` the
 * server sent, verbatim. This was live-bug territory — the gateway once
 * stored the number without its `+` (a Baileys jid has no `+` in its user
 * part), so this panel and `whatsapp-link-panel.tsx` both quietly rendered a
 * digit string with no plus sign. The fix belongs at the write, not here
 * (see `gateway/src/sessions.ts::jidToE164`), but this test still pins the
 * one contract this panel owns: show `phone_number` unmodified, `+` and all.
 *
 * `../../events` is stubbed for the same reason as `job-orders-claim.test.tsx`
 * — the live stream opens an `EventSource`, which jsdom does not have, and
 * nothing here is about the wire.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

vi.mock("../../events", () => ({
  useLive: () => {},
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function connectedSessionBody(phoneNumber: string | null) {
  return {
    status: "connected",
    qr: null,
    expires_at: null,
    phone_number: phoneNumber,
    connected_at: "2026-08-01T00:00:00Z",
    last_checked_at: "2026-08-01T00:00:00Z",
    notice_text: "Using WhatsApp this way risks a ban.",
    notice_version: "v1",
    risk_acknowledged_at: "2026-07-31T00:00:00Z",
    risk_notice_version: "v1",
  };
}

describe("WhatsappPanel", () => {
  it("shows the linked number with its leading +, as the server sent it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => connectedSessionBody("+6590034419"),
      } as Response),
    );

    render(<WhatsappPanel />);

    expect(await screen.findByText(/WhatsApp accepted the link for \+6590034419/)).toBeTruthy();
  });
});
