import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCandidates } from "./candidates";
import {
  ApiError,
  createClient,
  suspendClient,
  updateClient,
  useClients,
  type Client,
  type ClientPage,
} from "./clients";
import { CANDIDATES_PAGE_SIZE_KEY, CLIENTS_PAGE_SIZE_KEY } from "./use-persisted-page-size";

/** A real `Map`-backed `Storage` — jsdom's own localStorage persists across
 *  tests in the same run, and these tests depend on starting empty. */
function fakeStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: vi.fn((key: string) => (store.has(key) ? store.get(key)! : null)),
    setItem: vi.fn((key: string, value: string) => {
      store.set(key, value);
    }),
    removeItem: vi.fn((key: string) => {
      store.delete(key);
    }),
    clear: vi.fn(() => store.clear()),
    key: vi.fn(() => null),
    get length() {
      return store.size;
    },
  } as Storage;
}

function jsonResponse(body: unknown, init?: { status?: number }): Response {
  return {
    ok: (init?.status ?? 200) < 300,
    status: init?.status ?? 200,
    json: async () => body,
  } as Response;
}

function page(overrides: Partial<ClientPage> = {}): ClientPage {
  return {
    items: [],
    total: 0,
    limit: 50,
    offset: 0,
    counts: { all: 0 },
    initials: [],
    ...overrides,
  };
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
    logo_key: null,
    logo_updated_at: null,
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

describe("rows per page", () => {
  function lastUrl(fetchMock: ReturnType<typeof vi.fn>): string {
    const calls = fetchMock.mock.calls;
    return String(calls[calls.length - 1][0]);
  }

  it("sends the default limit until the reader asks for a different one", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page()));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useClients());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));

    // 50 is `CLIENTS_PAGE_SIZE` — the list's hardcoded limit before this
    // control existed, kept as the default so nobody's page changed size
    // the moment the selector shipped.
    expect(result.current.limit).toBe(50);
    expect(lastUrl(fetchMock)).toContain("limit=50");

    const callsBeforeResize = fetchMock.mock.calls.length;
    act(() => result.current.setLimit(100));
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("limit=100"));
    expect(result.current.limit).toBe(100);
    // One fetch for the size change, not two — see the identical assertion
    // in `candidates.test.ts`: a future refactor that split the limit change
    // and the offset reset into two effects would double-fetch here and
    // still pass a check of only the final URL.
    expect(fetchMock.mock.calls.length).toBe(callsBeforeResize + 1);
  });

  it("resets the page, so a larger page size cannot land on an offset past the end", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page({ total: 400 })));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useClients());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));

    act(() => result.current.setOffset(150));
    await waitFor(() => expect(result.current.offset).toBe(150));

    const callsBeforeResize = fetchMock.mock.calls.length;
    act(() => result.current.setLimit(100));
    await waitFor(() => expect(result.current.offset).toBe(0));
    expect(lastUrl(fetchMock)).toContain("offset=0");
    expect(lastUrl(fetchMock)).toContain("limit=100");
    expect(fetchMock.mock.calls.length).toBe(callsBeforeResize + 1);
  });
});

describe("rows per page persistence", () => {
  function lastUrl(fetchMock: ReturnType<typeof vi.fn>): string {
    const calls = fetchMock.mock.calls;
    return String(calls[calls.length - 1][0]);
  }

  it("remembers a chosen size across a remount — the thing the reader asked for", async () => {
    vi.stubGlobal("localStorage", fakeStorage());
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page()));
    vi.stubGlobal("fetch", fetchMock);

    const first = renderHook(() => useClients());
    await waitFor(() => expect(first.result.current.state.status).toBe("ready"));
    // 40 is on `CLIENTS_PAGE_SIZES` (10/20/30/40/50) — unlike the `100` the
    // test above uses to check the fetch URL, a persisted value has to
    // survive the ladder validation on the next mount too.
    act(() => first.result.current.setLimit(40));
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("limit=40"));
    first.unmount();

    const second = renderHook(() => useClients());
    await waitFor(() => expect(second.result.current.state.status).toBe("ready"));
    expect(second.result.current.limit).toBe(40);
    expect(lastUrl(fetchMock)).toContain("limit=40");
  });

  it("falls back to the default when the stored value is off the current ladder", async () => {
    const storage = fakeStorage();
    storage.setItem(CLIENTS_PAGE_SIZE_KEY, "999");
    vi.stubGlobal("localStorage", storage);
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page()));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useClients());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    expect(result.current.limit).toBe(50);
    expect(lastUrl(fetchMock)).toContain("limit=50");
  });

  it.each(["abc", "0", "-5", ""])("falls back to the default for garbage %j in storage", async (garbage) => {
    const storage = fakeStorage();
    storage.setItem(CLIENTS_PAGE_SIZE_KEY, garbage);
    vi.stubGlobal("localStorage", storage);
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page()));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useClients());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    expect(result.current.limit).toBe(50);
  });

  it("stays at the default and keeps working when localStorage throws on read and write", async () => {
    const throwing: Storage = {
      getItem: vi.fn(() => {
        throw new Error("storage blocked");
      }),
      setItem: vi.fn(() => {
        throw new Error("storage blocked");
      }),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(),
      length: 0,
    } as Storage;
    vi.stubGlobal("localStorage", throwing);
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page()));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useClients());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    expect(result.current.limit).toBe(50);

    expect(() => act(() => result.current.setLimit(40))).not.toThrow();
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("limit=40"));
    expect(result.current.limit).toBe(40);
  });

  it("does not collide with the candidates screen's stored size", async () => {
    vi.stubGlobal("localStorage", fakeStorage());
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ ...page(), items: [], counts: { all: 0 }, initials: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const clients = renderHook(() => useClients());
    await waitFor(() => expect(clients.result.current.state.status).toBe("ready"));
    act(() => clients.result.current.setLimit(40));
    await waitFor(() => expect(clients.result.current.limit).toBe(40));

    const candidates = renderHook(() => useCandidates());
    await waitFor(() => expect(candidates.result.current.limit).toBe(10));

    expect(localStorage.getItem(CLIENTS_PAGE_SIZE_KEY)).toBe("40");
    expect(localStorage.getItem(CANDIDATES_PAGE_SIZE_KEY)).toBeNull();
  });
});
