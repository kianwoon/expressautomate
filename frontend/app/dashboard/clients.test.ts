import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, createClient, suspendClient, updateClient, type Client } from "./clients";

function jsonResponse(body: unknown, init?: { status?: number }): Response {
  return {
    ok: (init?.status ?? 200) < 300,
    status: init?.status ?? 200,
    json: async () => body,
  } as Response;
}

function client(overrides: Partial<Client> = {}): Client {
  return {
    id: "c-1",
    name: "Acme Pte Ltd",
    name_normalized: "acme",
    email_domain: "acme.com",
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
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createClient", () => {
  it("posts to /api/clients with the given body", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(client(), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await createClient({ name: "Acme Pte Ltd", email_domain: "acme.com" });

    expect(result.id).toBe("c-1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/clients");
    expect(String(url)).not.toContain("/api/clients/");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ name: "Acme Pte Ltd", email_domain: "acme.com" });
  });

  it("surfaces the domain-collision detail on a 409", async () => {
    const detail =
      "Acme Holdings (other-id) already holds acme.com and is confirmed. " +
      "Open that client and edit it instead of adding a second one.";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail }, { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(createClient({ name: "Acme", email_domain: "acme.com" })).rejects.toEqual(
      new ApiError(detail),
    );
  });
});

describe("updateClient", () => {
  it("sends only the keys it was given, not a padded object", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(client({ notes: "VIP" })));
    vi.stubGlobal("fetch", fetchMock);

    await updateClient("c-1", { notes: "VIP" });

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/clients/c-1");
    expect(init.method).toBe("PATCH");
    const sent = JSON.parse(init.body);
    expect(sent).toEqual({ notes: "VIP" });
    expect(Object.keys(sent)).toEqual(["notes"]);
  });
});

describe("suspendClient", () => {
  it("posts the reason to /suspend", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ status: "suspended" }));
    vi.stubGlobal("fetch", fetchMock);

    await suspendClient("c-1", "Unpaid invoice");

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/clients/c-1/suspend");
    expect(JSON.parse(init.body)).toEqual({ reason: "Unpaid invoice" });
  });

  it("surfaces a suspension 409's detail distinctly from a submission 409", async () => {
    const suspendDetail = "Client is archived; restore it before marking it suspended";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: suspendDetail }, { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(suspendClient("c-1")).rejects.toEqual(new ApiError(suspendDetail));

    // Distinct from the two 409s on the submissions endpoint (already
    // suspended vs already submitted) — this wrapper must not collapse any
    // of these into one generic message.
    expect(suspendDetail).not.toBe("This client is already suspended.");
    expect(suspendDetail).not.toBe("This candidate was already submitted to this client.");
  });
});
