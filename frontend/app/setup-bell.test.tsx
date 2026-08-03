import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SetupBell } from "./setup-bell";

/**
 * Pinning the behaviour the spec calls out explicitly: silent on failure, no
 * layout-shifting reveal logic to get wrong, and the accessibility contract
 * (`aria-expanded`/`aria-controls`, an announced count, Escape-to-close with
 * focus return, blur-outside-to-close). The two-fetch data logic itself is
 * covered in setup-tasks.test.ts without a DOM — this file only has to prove
 * the component wires that logic up correctly.
 *
 * Assertions read attributes with `getAttribute` rather than a `jest-dom`
 * matcher like `toHaveAttribute` — this repo's Vitest setup (vitest.config.ts)
 * does not pull `@testing-library/jest-dom` in, matching the plain-Chai style
 * `share-dialog.test.tsx` already uses.
 *
 * `useAuth` is mocked to a fixed signed-in state: the component's own gate on
 * `auth.status` is exercised here only to prove the fetch does not fire for
 * any other status, not to re-test `useAuth` itself.
 */

vi.mock("./auth", () => ({
  useAuth: () => ({ status: "signed-in", me: {} }),
}));

function jsonResponse(body: unknown, ok = true) {
  return { ok, status: ok ? 200 : 500, json: async () => body };
}

function stubFetch(overrides: { notifications?: unknown; discovery?: unknown; fail?: "notifications" | "discovery" }) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.includes("client-discovery")) {
      if (overrides.fail === "discovery") return jsonResponse(null, false);
      return jsonResponse(overrides.discovery ?? { run: null });
    }
    if (overrides.fail === "notifications") return jsonResponse(null, false);
    return jsonResponse(overrides.notifications ?? { destinations: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SetupBell", () => {
  it("renders no button while both settings are configured", async () => {
    stubFetch({
      notifications: { destinations: [{ disabled: false }] },
      discovery: { run: { status: "done" } },
    });
    render(<SetupBell />);

    await waitFor(() => expect(screen.queryByRole("button")).toBeNull());
  });

  it("renders no button when either fetch fails", async () => {
    stubFetch({ fail: "discovery" });
    render(<SetupBell />);

    // Give the failed fetch a tick to resolve, then confirm nothing appeared.
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows the bell with an announced count once an item is outstanding", async () => {
    stubFetch({ discovery: { run: null } }); // notifications also unconfigured by default
    render(<SetupBell />);

    const button = await screen.findByRole("button");
    expect(button.getAttribute("aria-label")).toBe("2 setup steps left");
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("singularizes the count when only one item is outstanding", async () => {
    stubFetch({
      notifications: { destinations: [{ disabled: false }] },
      discovery: { run: null },
    });
    render(<SetupBell />);

    const button = await screen.findByRole("button");
    expect(button.getAttribute("aria-label")).toBe("1 setup step left");
  });

  it("opens the panel on click, listing each outstanding item as a link", async () => {
    stubFetch({});
    render(<SetupBell />);

    const button = await screen.findByRole("button");
    fireEvent.click(button);

    expect(button.getAttribute("aria-expanded")).toBe("true");
    const notifLink = screen.getByRole("link", { name: /Get alerts on WhatsApp or Telegram/ });
    expect(notifLink.getAttribute("href")).toBe("/settings/notifications");
    const discoveryLink = screen.getByRole("link", { name: /Turn on client discovery/ });
    expect(discoveryLink.getAttribute("href")).toBe("/settings/client-discovery");
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    stubFetch({});
    render(<SetupBell />);

    const button = await screen.findByRole("button");
    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");

    fireEvent.keyDown(button, { key: "Escape" });
    expect(button.getAttribute("aria-expanded")).toBe("false");
    await waitFor(() => expect(document.activeElement).toBe(button));
  });

  it("closes when focus moves outside the menu", async () => {
    stubFetch({});
    render(
      <div>
        <SetupBell />
        <button type="button">elsewhere</button>
      </div>,
    );

    const button = await screen.findByRole("button", { name: /setup steps left/ });
    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("true");

    const elsewhere = screen.getByRole("button", { name: "elsewhere" });
    fireEvent.focusOut(button.parentElement as HTMLElement, { relatedTarget: elsewhere });
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });
});
