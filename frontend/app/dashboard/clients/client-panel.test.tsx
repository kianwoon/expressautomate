import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Client } from "../clients";
import { ClientPanel } from "./client-panel";

/**
 * Pins the wiring itself, not just `ClientLogo` in isolation — the bug this
 * guards against (commit 74f991b's third variant, and the reload-on-every-
 * edit cascade before it) never lived inside the logo component. It lived in
 * which callback the panel handed the logo: uploading a logo must reach only
 * `onDetailChanged` (the single-record refetch), never `onChanged` (the
 * combined list-reload-plus-detail refresh `page.tsx` uses for confirm,
 * archive, restore and suspend). A test against `ClientLogo` alone, with an
 * unwired `reload` spy that nothing ever calls, cannot see that distinction —
 * that is the fake test this file replaces the intent of.
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

describe("ClientPanel logo wiring", () => {
  it("routes a logo upload to onDetailChanged alone, never onChanged", async () => {
    // Routed by URL rather than by call order: the panel also renders
    // `ClientAssignee`, which reads the staff list and the signed-in user. An
    // ordered `mockResolvedValueOnce` chain handed those two requests the
    // logo's responses and left the rest of the panel unrendered.
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).includes("/logo")) {
        return (init?.method ?? "GET") === "POST"
          ? jsonResponse({
              logo_key: "tenant/cl-1/logo.png",
              logo_updated_at: "2026-07-30T00:00:00Z",
            })
          : jsonResponse({ url: "https://r2.example/new.png", expires_in: 300 });
      }
      if (String(url).includes("/members")) return jsonResponse([]);
      return jsonResponse(null, { ok: false, status: 401 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const onChanged = vi.fn(); // stands in for `refreshDetail` (list reload + detail) in page.tsx
    const onDetailChanged = vi.fn(); // stands in for `refetchDetail` (detail only)

    render(
      <ClientPanel
        row={client()}
        onConfirm={async () => {}}
        onArchive={async () => {}}
        onRestore={async () => {}}
        onChanged={onChanged}
        onDetailChanged={onDetailChanged}
        onSelectClient={() => {}}
      />,
    );

    const file = new File(["bytes"], "logo.png", { type: "image/png" });
    const input = screen.getByLabelText("Add a logo of Meridian Partners") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });

    await screen.findByAltText("Logo of Meridian Partners");

    expect(onDetailChanged).toHaveBeenCalledTimes(1);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("routes Archive to onChanged, the combined list-and-detail refresh", async () => {
    const onChanged = vi.fn();
    const onDetailChanged = vi.fn();
    const onArchive = vi.fn().mockImplementation(async () => {
      onChanged();
    });

    render(
      <ClientPanel
        row={client()}
        onConfirm={async () => {}}
        onArchive={onArchive}
        onRestore={async () => {}}
        onChanged={onChanged}
        onDetailChanged={onDetailChanged}
        onSelectClient={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Archive" }));

    await vi.waitFor(() => expect(onArchive).toHaveBeenCalledTimes(1));

    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(onDetailChanged).not.toHaveBeenCalled();
  });
});
