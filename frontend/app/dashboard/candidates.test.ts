import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCandidates, type CandidatePage } from "./candidates";

/**
 * Pins the near-miss this test harness exists for (see frontend/CLAUDE.md
 * task notes): `?eligible_for=<opportunity>` makes the server send
 * `counts: null` and `initials: null` on purpose, because those numbers
 * describe the unfiltered population and must not sit above filtered rows.
 * `useCandidates` also keeps both sticky across refetches so the chrome
 * doesn't blink while loading. A plausible `setCounts(page.counts ?? counts)`
 * would satisfy the stickiness and reintroduce the bug — these tests fail
 * loudly if that ever creeps back in.
 */

function jsonResponse(body: unknown, init?: { status?: number }): Response {
  return {
    ok: (init?.status ?? 200) < 300,
    status: init?.status ?? 200,
    json: async () => body,
  } as Response;
}

function page(overrides: Partial<CandidatePage> = {}): CandidatePage {
  return {
    items: [],
    total: 0,
    limit: 10,
    offset: 0,
    counts: { all: 4200, new: 100 },
    initials: ["A", "B", "#"],
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the chips and letter bar go silent while filtering, not stale", () => {
  it("a real null from the server overwrites the sticky counts and initials", async () => {
    const fetchMock = vi
      .fn()
      // Plain list: real counts and initials.
      .mockResolvedValueOnce(jsonResponse(page()))
      // Filtered by eligible_for: the server's deliberate null.
      .mockResolvedValueOnce(
        jsonResponse(page({ counts: null, initials: null, excluded_ineligible: 5, scanned: 20 })),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCandidates());

    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    expect(result.current.counts).toEqual({ all: 4200, new: 100 });
    expect(result.current.initials).toEqual(["A", "B", "#"]);

    act(() => result.current.setEligibleFor("opp-1"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.counts).toBeNull());
    expect(result.current.initials).toBeNull();
  });

  it("a refetch that is merely in flight does not blank counts already delivered", async () => {
    let resolveSecond: (value: Response) => void = () => {};
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(page()))
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveSecond = resolve;
          }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCandidates());

    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    expect(result.current.counts).toEqual({ all: 4200, new: 100 });

    act(() => result.current.reload());

    await waitFor(() => expect(result.current.refreshing).toBe(true));
    // The second fetch has not resolved yet — the counts already on screen
    // must still be there, not cleared to show the reload is happening.
    expect(result.current.counts).toEqual({ all: 4200, new: 100 });
    expect(result.current.initials).toEqual(["A", "B", "#"]);

    await act(async () => {
      resolveSecond(jsonResponse(page({ counts: { all: 4201, new: 101 } })));
    });
    await waitFor(() => expect(result.current.refreshing).toBe(false));
  });

  it("clearing the eligibility filter restores counts and initials from the plain list", async () => {
    const fetchMock = vi
      .fn()
      // Mount already carries `eligible_for`, so the very first fetch is the
      // filtered one and comes back with the server's deliberate null.
      .mockResolvedValueOnce(jsonResponse(page({ counts: null, initials: null })))
      // Clearing the filter re-fetches the plain list.
      .mockResolvedValueOnce(jsonResponse(page({ counts: { all: 4200, new: 100 }, initials: ["A", "#"] })));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCandidates("opp-1"));

    // First render already carries eligible_for, so the very first fetch is
    // the filtered one and the server's null must show up immediately.
    await waitFor(() => expect(result.current.state.status).toBe("ready"));
    await waitFor(() => expect(result.current.counts).toBeNull());
    expect(result.current.initials).toBeNull();

    act(() => result.current.setEligibleFor(null));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.counts).toEqual({ all: 4200, new: 100 }));
    expect(result.current.initials).toEqual(["A", "#"]);
  });

  it("a 409 with reason placement_type_not_set puts the hook in its blocked state", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(
      jsonResponse(
        { detail: "This job order needs a placement type first.", reason: "placement_type_not_set" },
        { status: 409 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCandidates("opp-1"));

    await waitFor(() => expect(result.current.state.status).toBe("blocked"));
    if (result.current.state.status !== "blocked") throw new Error("unreachable");
    expect(result.current.state.reason).toBe("placement_type_not_set");
    expect(result.current.state.detail).toBe("This job order needs a placement type first.");
    // Never a ready state carrying an unfiltered list in disguise.
    expect(result.current.state.status).not.toBe("ready");
  });
});
