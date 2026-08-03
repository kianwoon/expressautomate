import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IntakePause } from "./intake-pause";

/**
 * The intake switch (spec: 2026-08-03-mailbox-intake-pause-design.md).
 *
 * The behaviours pinned here are the ones the spec names for this component:
 * both states render in words, the toggle calls the route, a paused mailbox
 * reads as paused on a fresh load, and resuming warns before acting — the
 * warning being the one chance to learn that paused-window mail is gone.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

const PAUSED_AT = "2026-07-28T09:00:00+00:00";

function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  };
}

type AnyMock = ReturnType<typeof vi.fn>;

/** Stub the settings read and both toggle routes in one place. */
function mockApi({ paused = null as string | null, settingsStatus = 200 } = {}) {
  const fetchMock = vi.fn(async (url: unknown, init?: { method?: string }) => {
    const path = String(url);
    if (path.endsWith("/api/mailbox/pause") && init?.method === "POST") {
      return jsonResponse({ paused_at: PAUSED_AT });
    }
    if (path.endsWith("/api/mailbox/resume") && init?.method === "POST") {
      return jsonResponse({ resumed_from: "2026-08-11T08:00:00+00:00" });
    }
    if (settingsStatus !== 200) {
      return jsonResponse({ detail: "no" }, { ok: false, status: settingsStatus });
    }
    return jsonResponse({
      initial_sync_from: "2026-05-01T00:00:00+00:00",
      backfill_complete: true,
      ingest_paused_at: paused,
      options: [],
    });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
  return fetchMock;
}

function calls(fetchMock: AnyMock, suffix: string): number {
  return fetchMock.mock.calls.filter(([url]) => String(url).endsWith(suffix)).length;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("IntakePause", () => {
  it("says intake is on, in words, when nothing is paused", async () => {
    mockApi();
    render(<IntakePause />);

    expect(await screen.findByText("Intake on")).toBeTruthy();
  });

  it("reads as paused on a fresh load — the state survives a reload", async () => {
    // The component holds no state of its own worth trusting: the server's
    // `ingest_paused_at` is the fact, and a reload must render it.
    mockApi({ paused: PAUSED_AT });
    render(<IntakePause />);

    expect(await screen.findByText(/Intake paused since/)).toBeTruthy();
  });

  it("renders nothing when there is no mailbox to pause", async () => {
    // A 404 from settings means onboarding never finished. A switch wired to
    // nothing would 404 on every click; better absent than broken.
    mockApi({ settingsStatus: 404 });
    const { container } = render(<IntakePause />);

    await waitFor(() => expect(container.querySelector(".jo-intake")).toBeNull());
  });

  it("pausing calls the route and flips the label to paused", async () => {
    const fetchMock = mockApi();
    render(<IntakePause />);

    fireEvent.click(await screen.findByText("Intake on"));

    await waitFor(() => expect(calls(fetchMock, "/api/mailbox/pause")).toBe(1));
    expect(await screen.findByText(/Intake paused since/)).toBeTruthy();
  });

  it("resuming warns first and does nothing until confirmed", async () => {
    // Mail received during the pause is never picked up. That sentence has to
    // be read *before* the resume, because afterwards is too late to matter.
    const fetchMock = mockApi({ paused: PAUSED_AT });
    render(<IntakePause />);

    fireEvent.click(await screen.findByText(/Intake paused since/));

    expect(await screen.findByText(/not.*picked up|will not be picked up/i)).toBeTruthy();
    expect(calls(fetchMock, "/api/mailbox/resume")).toBe(0);

    fireEvent.click(screen.getByRole("button", { name: "Resume from now" }));

    await waitFor(() => expect(calls(fetchMock, "/api/mailbox/resume")).toBe(1));
    expect(await screen.findByText("Intake on")).toBeTruthy();
  });

  it("staying paused closes the warning without calling the route", async () => {
    const fetchMock = mockApi({ paused: PAUSED_AT });
    render(<IntakePause />);

    fireEvent.click(await screen.findByText(/Intake paused since/));
    fireEvent.click(await screen.findByRole("button", { name: "Stay paused" }));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Resume from now" })).toBeNull(),
    );
    expect(calls(fetchMock, "/api/mailbox/resume")).toBe(0);
    expect(screen.getByText(/Intake paused since/)).toBeTruthy();
  });

  it("a failed resume keeps saying paused, in the server's words", async () => {
    // The server's detail names the truth — still paused — and the pill must
    // not claim otherwise on its own authority.
    const fetchMock = mockApi({ paused: PAUSED_AT });
    fetchMock.mockImplementation(async (url: unknown, init?: { method?: string }) => {
      const path = String(url);
      if (path.endsWith("/api/mailbox/resume") && init?.method === "POST") {
        return jsonResponse(
          { detail: "Microsoft could not be reached just now — intake is still paused." },
          { ok: false, status: 502 },
        );
      }
      return jsonResponse({
        initial_sync_from: "2026-05-01T00:00:00+00:00",
        backfill_complete: true,
        ingest_paused_at: PAUSED_AT,
        options: [],
      });
    });
    render(<IntakePause />);

    fireEvent.click(await screen.findByText(/Intake paused since/));
    fireEvent.click(await screen.findByRole("button", { name: "Resume from now" }));

    expect(await screen.findByText(/intake is still paused/)).toBeTruthy();
    expect(screen.getByText(/Intake paused since/)).toBeTruthy();
  });
});
