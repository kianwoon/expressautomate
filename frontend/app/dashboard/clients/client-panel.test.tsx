import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  it("opens in edit mode: the form is shown before the read-only record", () => {
    // The modal's default is edit-on-by-default — a recruiter who clicked a
    // row did so to act on it, and Save changes is that action. The read-only
    // half (facts, mentions, the action buttons) waits behind one Cancel.
    render(
      <ClientPanel
        row={client()}
        onClose={() => {}}
        onConfirm={async () => {}}
        onArchive={async () => {}}
        onRestore={async () => {}}
        onChanged={() => {}}
        onDetailChanged={() => {}}
        onSelectClient={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: "Save changes" })).toBeTruthy();
    expect(screen.queryByText("Looked after by")).toBeNull();
  });

  it("Cancel leaves edit mode and shows the read-only record", () => {
    render(
      <ClientPanel
        row={client()}
        onClose={() => {}}
        onConfirm={async () => {}}
        onArchive={async () => {}}
        onRestore={async () => {}}
        onChanged={() => {}}
        onDetailChanged={() => {}}
        onSelectClient={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByText("Looked after by")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
  });

  it("Save changes closes the modal — the edit is the whole interaction", async () => {
    // Pins the reported bug: after a save, the modal used to flip to the
    // read-only record, whose Confirm button read as "confirm the save" and
    // moved an unconfirmed client out of the Unconfirmed queue — the client
    // "disappeared". Saving must close the modal and offer nothing after it.
    const onClose = vi.fn();
    const onChanged = vi.fn();
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(client()));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ClientPanel
        row={client()}
        onClose={onClose}
        onConfirm={async () => {}}
        onArchive={async () => {}}
        onRestore={async () => {}}
        onChanged={onChanged}
        onDetailChanged={() => {}}
        onSelectClient={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText("Phone"), { target: { value: "+65 9999 8888" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onChanged).toHaveBeenCalledTimes(1);
    // And the read-only half with its Confirm button never appears.
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });

  it("offers Confirm beside Save changes in the title row for an unconfirmed client", () => {
    // The confirm action lives in the header now, next to Save — not hidden
    // behind the read-only half. A recruiter who opened an unconfirmed client
    // to edit it can graduate it out of the queue from the same row of
    // buttons they saved with.
    const onConfirm = vi.fn();
    render(
      <ClientPanel
        row={client({ status: "unconfirmed" })}
        onClose={() => {}}
        onConfirm={onConfirm}
        onArchive={async () => {}}
        onRestore={async () => {}}
        onChanged={() => {}}
        onDetailChanged={() => {}}
        onSelectClient={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: "Save changes" })).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "Confirm" });
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

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
        onClose={() => {}}
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
        // Read-only half pinned: the panel opens in edit mode by default, and
        // the action buttons are the read-only half's — see `defaultEditing`.
        defaultEditing={false}
        onClose={() => {}}
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
