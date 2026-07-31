import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { canEditCandidate, useCandidates, type CandidatePage } from "./candidates";

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

    // The mock answers by call order, so without this every assertion above
    // would still hold for a hook that had quietly stopped sending
    // `eligible_for` at all — it would be handed the filtered payload
    // regardless, and the null counts would look like proof of a filter that
    // was never applied. Pin the request, not just the response.
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("eligible_for");
    expect(String(fetchMock.mock.calls[1][0])).toContain("eligible_for=opp-1");
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

/**
 * `scope` is the parameter that makes "Everyone" honest.
 *
 * "All candidates" was never everything — it was always everything this
 * recruiter is allowed to see — and the server has said so since
 * `candidate_scope` shipped. What was missing was any way to ask the other
 * three questions, so the list could not distinguish "the agency has nobody"
 * from "I hold nobody".
 */
describe("whose candidates", () => {
  function lastUrl(fetchMock: ReturnType<typeof vi.fn>): string {
    const calls = fetchMock.mock.calls;
    return String(calls[calls.length - 1][0]);
  }

  it("sends nothing at all for the default, and the value for the rest", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page()));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCandidates());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));

    // `all` is the server's own default; sending it would put a redundant
    // parameter on every ordinary request.
    expect(result.current.scope).toBe("all");
    expect(lastUrl(fetchMock)).not.toContain("scope=");

    act(() => result.current.setScope("mine"));
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("scope=mine"));

    act(() => result.current.setScope("shared_with_me"));
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("scope=shared_with_me"));

    act(() => result.current.setScope("all"));
    await waitFor(() => expect(lastUrl(fetchMock)).not.toContain("scope="));
  });

  it("resets the page, so a narrower scope cannot land on an empty offset", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page({ total: 400 })));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCandidates());
    await waitFor(() => expect(result.current.state.status).toBe("ready"));

    act(() => result.current.setOffset(150));
    await waitFor(() => expect(result.current.offset).toBe(150));

    // Staying on offset 150 of five matching rows reads exactly like "you
    // hold nobody", which is the misreading this whole row exists to prevent.
    act(() => result.current.setScope("queue"));
    await waitFor(() => expect(result.current.offset).toBe(0));
    expect(lastUrl(fetchMock)).toContain("offset=0");
  });
});

/**
 * `can_edit` now ships on every candidate row (2c6051f), computed server-side
 * by `can_edit_candidate` — the owner, or an agency owner. The UI must not
 * re-derive that rule from `owner` + a role check: a second copy of "who may
 * edit" is a second place for it to drift from the server's. So this reads
 * `row.can_edit` and nothing else — no `me`, no role, no owner id comparison.
 *
 * Fails CLOSED on a missing value. The old version treated `undefined` as
 * "the server did not say" and refused nothing, because the field genuinely
 * did not exist yet. It exists on every row now, so a candidate object
 * without it is not the server being silent by design — it is stale or
 * malformed data, and guessing "editable" for a permission flag is the wrong
 * default to guess.
 */
describe("canEditCandidate", () => {
  it("is exactly what the server says, true or false", () => {
    expect(canEditCandidate({ can_edit: true })).toBe(true);
    expect(canEditCandidate({ can_edit: false })).toBe(false);
  });

  it("fails closed when the server did not send a value", () => {
    expect(canEditCandidate({} as { can_edit: boolean })).toBe(false);
  });
});
