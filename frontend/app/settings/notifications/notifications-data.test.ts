import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NOTIFICATIONS_SETTINGS_PATH } from "../../api";
import { useNotifications, type NotificationSettings } from "./notifications-data";

/**
 * `addWhatsAppLinkedDestination`'s status mapping.
 *
 * This is the regression this test exists to catch: the 400 case was
 * originally written as 409 and later corrected. `whatsapp-link-panel.test.tsx`
 * only checks that the panel surfaces whatever sentence the write resolves
 * to — it never exercises a real status code — so the mapping itself has to
 * be tested here, against the hook, or a revert back to 409 would ship silently.
 */

function jsonResponse(body: unknown, init?: { status?: number }): Response {
  return {
    ok: (init?.status ?? 200) < 300,
    status: init?.status ?? 200,
    json: async () => body,
  } as Response;
}

function settings(overrides: Partial<NotificationSettings> = {}): NotificationSettings {
  return {
    channels: {},
    whatsapp_linked_number: "+6591234567",
    destinations: [],
    events: [],
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("addWhatsAppLinkedDestination", () => {
  it("maps a 400 to the connected-device sentence", async () => {
    const fetchMock = vi
      .fn()
      // Initial settings load fired by the hook's own effect.
      .mockResolvedValueOnce(jsonResponse(settings()))
      .mockResolvedValueOnce(jsonResponse({}, { status: 400 }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));

    let message: string | null = null;
    await act(async () => {
      message = await result.current.addWhatsAppLinkedDestination();
    });

    expect(message).toBe(
      "You need a connected WhatsApp device first — link one in Settings -> WhatsApp.",
    );
    // No re-read on failure.
    expect(fetchMock.mock.calls.length).toBe(2);
  });

  it("returns null and re-reads settings on success", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(settings()))
      .mockResolvedValueOnce(jsonResponse({}, { status: 200 }))
      .mockResolvedValueOnce(jsonResponse(settings({ whatsapp_linked_number: "+6591234567" })));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));

    let message: string | null = null;
    await act(async () => {
      message = await result.current.addWhatsAppLinkedDestination();
    });

    expect(message).toBeNull();
    // The initial load, the write, and the write's own follow-up re-read —
    // the same pattern `setEvents` and `unlink` follow. Dropping the re-read
    // would leave this at 2.
    expect(fetchMock.mock.calls.length).toBe(3);
    // And that it re-read the SETTINGS endpoint. A count alone would pass if
    // the follow-up read went somewhere else entirely.
    expect(fetchMock.mock.calls[2][0]).toBe(NOTIFICATIONS_SETTINGS_PATH);
  });

  it("maps a 500 to the generic sentence and does not re-read", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(settings()))
      .mockResolvedValueOnce(jsonResponse({}, { status: 500 }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));

    let message: string | null = null;
    await act(async () => {
      message = await result.current.addWhatsAppLinkedDestination();
    });

    expect(message).toBe("We could not add that just now.");
    expect(fetchMock.mock.calls.length).toBe(2);
  });

  it("maps a thrown network failure to the could-not-reach sentence", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(settings()))
      .mockRejectedValueOnce(new TypeError("network down"));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useNotifications());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));

    let message: string | null = null;
    await act(async () => {
      message = await result.current.addWhatsAppLinkedDestination();
    });

    expect(message).toBe("We could not reach the server.");
    expect(fetchMock.mock.calls.length).toBe(2);
  });
});
