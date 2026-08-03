import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePersistedPageSize } from "./use-persisted-page-size";

const LADDER = [10, 20, 30, 50] as const;
const DEFAULT = 10;

/** A real `Map`-backed `Storage`, not jsdom's — jsdom's own localStorage
 *  persists across tests in the same run, and this suite depends on starting
 *  empty every time. */
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

/** Safari private mode, a full quota, or storage disabled outright all
 *  surface the same way: the call throws rather than failing quietly. */
function throwingStorage(): Storage {
  return {
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
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("usePersistedPageSize", () => {
  it("remembers a chosen size across a remount", () => {
    vi.stubGlobal("localStorage", fakeStorage());

    const { result, unmount } = renderHook(() =>
      usePersistedPageSize("k.remember", DEFAULT, LADDER),
    );
    act(() => result.current[1](30));
    expect(result.current[0]).toBe(30);
    unmount();

    const remount = renderHook(() => usePersistedPageSize("k.remember", DEFAULT, LADDER));
    expect(remount.result.current[0]).toBe(30);
  });

  it("falls back to the default when the stored value is off the ladder", () => {
    const storage = fakeStorage();
    storage.setItem("k.offladder", "999");
    vi.stubGlobal("localStorage", storage);

    const { result } = renderHook(() => usePersistedPageSize("k.offladder", DEFAULT, LADDER));
    expect(result.current[0]).toBe(DEFAULT);
  });

  it.each(["abc", "0", "-5", ""])("falls back to the default for garbage %j", (garbage) => {
    const storage = fakeStorage();
    storage.setItem("k.garbage", garbage);
    vi.stubGlobal("localStorage", storage);

    const { result } = renderHook(() => usePersistedPageSize("k.garbage", DEFAULT, LADDER));
    expect(result.current[0]).toBe(DEFAULT);
  });

  it("stays at the default when reading storage throws", () => {
    vi.stubGlobal("localStorage", throwingStorage());

    const { result } = renderHook(() => usePersistedPageSize("k.readthrow", DEFAULT, LADDER));
    expect(result.current[0]).toBe(DEFAULT);
  });

  it("keeps the control working for the session when writing storage throws", () => {
    vi.stubGlobal("localStorage", throwingStorage());

    const { result } = renderHook(() => usePersistedPageSize("k.writethrow", DEFAULT, LADDER));
    expect(() => act(() => result.current[1](20))).not.toThrow();
    expect(result.current[0]).toBe(20);
  });

  it("keeps two screens' storage keys independent", () => {
    const storage = fakeStorage();
    vi.stubGlobal("localStorage", storage);

    const candidates = renderHook(() =>
      usePersistedPageSize("k.candidates", DEFAULT, LADDER),
    );
    const clients = renderHook(() => usePersistedPageSize("k.clients", DEFAULT, LADDER));

    act(() => candidates.result.current[1](30));
    // Setting candidates wrote only its own key — the clients hook, already
    // mounted at the default, is untouched by it.
    expect(clients.result.current[0]).toBe(DEFAULT);
    expect(storage.getItem("k.clients")).toBeNull();

    // And a fresh mount of clients still comes up at the default, not at
    // whatever candidates just remembered.
    const remountedClients = renderHook(() =>
      usePersistedPageSize("k.clients", DEFAULT, LADDER),
    );
    expect(remountedClients.result.current[0]).toBe(DEFAULT);
  });
});
