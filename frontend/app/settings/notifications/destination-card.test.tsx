import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DestinationCard } from "./destination-card";
import type { Destination, NotificationEvent } from "./notifications-data";

/**
 * The generic destination card, exercised specifically for the new
 * `whatsapp_linked` channel — it must read as "WhatsApp" (a human label, not
 * the raw wire key) and behave identically to any other channel: same event
 * checkboxes, same Unlink.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const EVENTS: NotificationEvent[] = [{ kind: "opportunity.new" }];

const WHATSAPP_LINKED: Destination = {
  id: "d1",
  channel: "whatsapp_linked",
  scope: "user",
  verified: true,
  disabled: false,
  event_kinds: [],
};

describe("DestinationCard, channel whatsapp_linked", () => {
  it("labels the card WhatsApp, not the raw channel key", () => {
    render(
      <DestinationCard
        destination={WHATSAPP_LINKED}
        events={EVENTS}
        onSetEvents={vi.fn()}
        onUnlink={vi.fn()}
      />,
    );

    expect(screen.getByText("WhatsApp")).toBeTruthy();
    expect(screen.queryByText("whatsapp_linked")).toBeNull();
  });

  it("ticking an event calls onSetEvents with the full set", async () => {
    const onSetEvents = vi.fn(async () => null);
    render(
      <DestinationCard
        destination={WHATSAPP_LINKED}
        events={EVENTS}
        onSetEvents={onSetEvents}
        onUnlink={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox"));

    expect(onSetEvents).toHaveBeenCalledWith(["opportunity.new"]);
  });

  it("Unlink asks first, then calls onUnlink on confirmation", async () => {
    const onUnlink = vi.fn(async () => null);
    render(
      <DestinationCard
        destination={WHATSAPP_LINKED}
        events={EVENTS}
        onSetEvents={vi.fn()}
        onUnlink={onUnlink}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Unlink" }));
    expect(onUnlink).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Yes, unlink" }));
    expect(onUnlink).toHaveBeenCalledTimes(1);
  });
});
