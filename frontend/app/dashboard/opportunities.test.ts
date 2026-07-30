import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { assignOpportunity, claimOpportunity, useOpportunities } from "./opportunities";

/**
 * The scope parameter and the two mutations behind the assignment UI.
 *
 * The status codes are the point. A 409 is two recruiters reaching for the
 * same job order at the same moment — an ordinary race with its own sentence,
 * not a failure. Collapsing it into "something went wrong" throws away the
 * only thing the backend went to the trouble of telling us.
 *
 * allow-hardcode: the strings asserted here are the user-facing copy under
 * test, not configuration.
 */

function jsonResponse(body: unknown, init?: { status?: number }): Response {
  return {
    ok: (init?.status ?? 200) < 300,
    status: init?.status ?? 200,
    json: async () => body,
  } as Response;
}

function page(overrides: Record<string, unknown> = {}) {
  return {
    items: [],
    total: 0,
    limit: 10,
    offset: 0,
    counts: { all: 0, new: 0, needs_review: 0, reviewed: 0 },
    ...overrides,
  };
}

/** The URL of the most recent list request. The counts refresh uses `limit=1`,
 *  and a poll can land at any time, so "the last fetch" is read here rather
 *  than assumed to be the one the test just caused. */
function lastUrl(fetchMock: ReturnType<typeof vi.fn>): string {
  return String(fetchMock.mock.calls[fetchMock.mock.calls.length - 1]?.[0] ?? "");
}

function listFetch() {
  return vi.fn().mockResolvedValue(jsonResponse(page()));
}

/** jsdom has no `EventSource`, and the hook opens the live stream on mount.
 *  A stub that does nothing keeps these tests about the query string rather
 *  than about server-sent events. */
class SilentEventSource {
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): void {}
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the scope parameter", () => {
  it("puts the scope in the query string", async () => {
    // Driven explicitly. Asserting against the default would pass whether or
    // not the parameter is ever wired, because `all` is deliberately omitted.
    const fetchMock = listFetch();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", SilentEventSource);

    const { result } = renderHook(() => useOpportunities());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    act(() => result.current.setScope("queue"));
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("scope=queue"));
  });

  it("omits the parameter entirely for the default scope", async () => {
    const fetchMock = listFetch();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", SilentEventSource);

    const { result } = renderHook(() => useOpportunities());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    expect(result.current.scope).toBe("all");
    expect(lastUrl(fetchMock)).not.toContain("scope=");
  });

  it("returns to the first page when the scope changes", async () => {
    // Switching to a short list while deep in a long one would otherwise land
    // on an empty page, which reads exactly like "there are none".
    const fetchMock = listFetch();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", SilentEventSource);

    const { result } = renderHook(() => useOpportunities());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    act(() => result.current.setOffset(150));
    await waitFor(() => expect(result.current.offset).toBe(150));

    act(() => result.current.setScope("mine"));
    await waitFor(() => expect(result.current.offset).toBe(0));
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("offset=0"));
    expect(lastUrl(fetchMock)).toContain("scope=mine");
  });
});

describe("claiming a job order", () => {
  it("reports a 409 claim as a conflict, not a generic failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({}, { status: 409 })));
    expect(await claimOpportunity("abc")).toEqual({
      ok: false,
      kind: "conflict",
      message: "Someone else has taken this one.",
    });
  });

  it("reports a 404 as no-longer-available", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({}, { status: 404 })));
    expect(await claimOpportunity("abc")).toEqual({
      ok: false,
      kind: "gone",
      message: "This job order is no longer available.",
    });
  });

  it("reports success plainly", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);

    expect(await claimOpportunity("a b")).toEqual({ ok: true });
    expect(String(fetchMock.mock.calls[0][0])).toContain("/opportunities/a%20b/claim");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
  });
});

describe("assigning a job order", () => {
  it("assign with null releases the job order", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);

    expect(await assignOpportunity("abc", null)).toEqual({ ok: true });
    expect(fetchMock.mock.calls[0][1].body).toBe(JSON.stringify({ user_id: null }));
  });

  it("sends the chosen recruiter's id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);

    expect(await assignOpportunity("abc", "user-1")).toEqual({ ok: true });
    expect(fetchMock.mock.calls[0][1].body).toBe(JSON.stringify({ user_id: "user-1" }));
  });

  it("says a 403 is someone else's job order rather than a failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({}, { status: 403 })));
    expect(await assignOpportunity("abc", null)).toEqual({
      ok: false,
      kind: "forbidden",
      message: "This job order is not yours to reassign.",
    });
  });

  it("reports an unreachable server without claiming a conflict", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    expect(await assignOpportunity("abc", "user-1")).toEqual({
      ok: false,
      kind: "failed",
      message: "We could not reach the server. Nothing has changed.",
    });
  });
});
