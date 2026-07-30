import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Client } from "../clients";
import { ClientLogo } from "./client-logo";

/**
 * Follows `client-form.test.tsx`'s setup: a fake `fetch`, a fixture client,
 * and assertions against what actually left the component rather than
 * against implementation detail.
 *
 * allow-hardcode: the strings below are test fixtures.
 */

function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  } as Response;
}

function client(overrides: Partial<Client> = {}): Client {
  return {
    id: "cl-1",
    name: "Meridian Partners",
    name_normalized: "meridian partners",
    email_domain: "meridianpartners.com",
    status: "confirmed",
    merged_into_client_id: null,
    last_seen_at: null,
    created_at: "2026-07-30T00:00:00Z",
    website: null,
    phone: null,
    address: null,
    fee_percent: null,
    payment_terms_days: null,
    notes: null,
    source: "manual",
    suspended_reason: null,
    suspended_at: null,
    logo_key: null,
    logo_updated_at: null,
    mentions: [],
    contacts: [],
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ClientLogo", () => {
  it("renders the initials and fires no logo request when there is no logo", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<ClientLogo client={client()} />);

    await screen.findByText("MP");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches the presigned URL once and puts it on the image", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ url: "https://r2.example/logo.png?sig=abc", expires_in: 300 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<ClientLogo client={client({ logo_key: "tenant/cl-1/logo.png" })} />);

    const img = await screen.findByAltText("Logo of Meridian Partners");
    expect(img.getAttribute("src")).toBe("https://r2.example/logo.png?sig=abc");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/clients/cl-1/logo");
  });

  it("uploads the chosen file, shows the new logo, and makes no further request or list-reload call", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ logo_key: "tenant/cl-1/logo.png", logo_updated_at: "2026-07-30T00:00:00Z" }),
      )
      .mockResolvedValueOnce(jsonResponse({ url: "https://r2.example/new.png", expires_in: 300 }));
    vi.stubGlobal("fetch", fetchMock);

    // Stands in for `reload()` from `useClients` — the regression this test
    // pins is that uploading a logo must never trigger a full list refetch.
    const reload = vi.fn();
    render(<ClientLogo client={client()} />);

    await screen.findByText("MP");

    const file = new File(["bytes"], "logo.png", { type: "image/png" });
    const input = screen.getByLabelText("Add a logo of Meridian Partners") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });

    const img = await screen.findByAltText("Logo of Meridian Partners");
    expect(img.getAttribute("src")).toBe("https://r2.example/new.png");

    // Upload (POST) + one re-presign to show the result — exactly two calls,
    // nothing more. A third call would be the redundant re-presign this fix
    // removes; a list reload would show up here too since both would have to
    // go through the same `fetch`.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(reload).not.toHaveBeenCalled();

    const uploadCall = fetchMock.mock.calls[0];
    expect(uploadCall[0]).toBe("/api/clients/cl-1/logo");
    expect(uploadCall[1].method).toBe("POST");
    expect(uploadCall[1].body).toBeInstanceOf(FormData);
  });

  it("deletes the logo, returns to initials, and makes no further request or list-reload call", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ url: "https://r2.example/logo.png", expires_in: 300 }))
      .mockResolvedValueOnce(jsonResponse(null, { ok: true, status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    const reload = vi.fn();
    render(<ClientLogo client={client({ logo_key: "tenant/cl-1/logo.png" })} />);

    await screen.findByAltText("Logo of Meridian Partners");

    fireEvent.click(screen.getByRole("button", { name: "Remove the logo of Meridian Partners" }));

    await screen.findByText("MP");

    // Initial presign (GET) + the DELETE — exactly two calls. No third call
    // to re-presign an image that no longer exists, and no list reload.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][1].method).toBe("DELETE");
    expect(reload).not.toHaveBeenCalled();
  });
});
