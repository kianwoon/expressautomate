import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Destination } from "./notifications-data";
import { WhatsAppLinkPanel } from "./whatsapp-link-panel";

/**
 * The WhatsApp-linked destination panel (mirrors Telegram's, minus the QR).
 *
 * Pinned here: it offers Add once a device is linked and no destination
 * exists yet, hides once one does, points to Settings -> WhatsApp when not
 * linked, and surfaces whatever sentence `onAdd` resolves to inline. The
 * status-code mapping that produces that sentence (400 -> the connected-device
 * copy, below) is the panel's caller's job and is tested in
 * `notifications-data.test.ts`, not here.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const NO_DESTINATIONS: Destination[] = [];
const WHATSAPP_LINKED_DESTINATION: Destination[] = [
  { id: "d1", channel: "whatsapp_linked", scope: "user", verified: true, disabled: false, event_kinds: [] },
];

describe("WhatsAppLinkPanel", () => {
  it("offers Add when a device is linked and no destination exists yet", async () => {
    render(
      <WhatsAppLinkPanel
        linked
        linkedNumber="+6591234567"
        destinations={NO_DESTINATIONS}
        onAdd={vi.fn()}
      />,
    );

    expect(screen.getByText(/\+6591234567/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Add" })).toBeTruthy();
  });

  it("hides once a whatsapp_linked destination exists", () => {
    const { container } = render(
      <WhatsAppLinkPanel
        linked
        linkedNumber="+6591234567"
        destinations={WHATSAPP_LINKED_DESTINATION}
        onAdd={vi.fn()}
      />,
    );

    expect(container.firstChild).toBeNull();
  });

  it("shows the go-pair-first message when not linked", () => {
    render(
      <WhatsAppLinkPanel linked={false} linkedNumber={null} destinations={NO_DESTINATIONS} onAdd={vi.fn()} />,
    );

    expect(screen.getByText(/Settings -> WhatsApp/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull();
  });

  it("surfaces whatever sentence the write resolves to", async () => {
    const onAdd = vi.fn(async () => "You need a connected WhatsApp device first — link one in Settings -> WhatsApp.");
    render(
      <WhatsAppLinkPanel
        linked
        linkedNumber="+6591234567"
        destinations={NO_DESTINATIONS}
        onAdd={onAdd}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText(/link one in Settings -> WhatsApp/)).toBeTruthy();
    expect(onAdd).toHaveBeenCalledTimes(1);
  });
});
