import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchOutstandingSetupTasks, outstandingTasks } from "./setup-tasks";

/**
 * The pure decision (`outstandingTasks`) is tested with hand-built response
 * fixtures and no DOM or network at all — that is the whole reason the logic
 * is split out of the fetch. `fetchOutstandingSetupTasks` gets its own,
 * smaller set of tests for the two things only the fetch layer can get wrong:
 * folding an HTTP failure into `null`, and not swallowing the two calls into
 * one path that only checks the first response.
 */
describe("outstandingTasks", () => {
  function notifications(destinations: { disabled: boolean }[]) {
    return { destinations };
  }

  it("lists both items when neither is configured", () => {
    const tasks = outstandingTasks(notifications([]), { run: null });
    expect(tasks.map((t) => t.id)).toEqual(["notifications", "client-discovery"]);
  });

  it("drops the notifications item once any destination is enabled", () => {
    const tasks = outstandingTasks(
      notifications([{ disabled: true }, { disabled: false }]),
      { run: null },
    );
    expect(tasks.map((t) => t.id)).toEqual(["client-discovery"]);
  });

  it("counts an all-disabled destination list as not configured", () => {
    const tasks = outstandingTasks(notifications([{ disabled: true }]), { run: null });
    expect(tasks.map((t) => t.id)).toContain("notifications");
  });

  it("drops the client-discovery item once a run exists, even a failed one", () => {
    const tasks = outstandingTasks(notifications([]), {
      run: { status: "failed" },
    });
    expect(tasks.map((t) => t.id)).toEqual(["notifications"]);
  });

  it("is empty once both are configured", () => {
    const tasks = outstandingTasks(notifications([{ disabled: false }]), {
      run: { status: "done" },
    });
    expect(tasks).toEqual([]);
  });

  it("every outstanding task links to its own settings page", () => {
    const tasks = outstandingTasks(notifications([]), { run: null });
    for (const task of tasks) {
      expect(task.href).toMatch(/^\/settings\//);
    }
  });
});

describe("fetchOutstandingSetupTasks", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function jsonResponse(body: unknown, ok = true) {
    return { ok, status: ok ? 200 : 500, json: async () => body };
  }

  it("returns the outstanding items when both reads succeed", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("client-discovery")) return jsonResponse({ run: null });
      return jsonResponse({ destinations: [] });
    });
    vi.stubGlobal("fetch", fetchMock);

    const tasks = await fetchOutstandingSetupTasks();
    expect(tasks?.map((t) => t.id)).toEqual(["notifications", "client-discovery"]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("returns null when the notifications read fails, even if discovery succeeds", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("client-discovery")) return jsonResponse({ run: null });
      return jsonResponse(null, false);
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchOutstandingSetupTasks()).toBeNull();
  });

  it("returns null when the discovery read fails, even if notifications succeeds", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("client-discovery")) return jsonResponse(null, false);
      return jsonResponse({ destinations: [] });
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchOutstandingSetupTasks()).toBeNull();
  });

  it("returns null rather than throwing when fetch itself rejects", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("network down");
      }),
    );

    expect(await fetchOutstandingSetupTasks()).toBeNull();
  });
});
