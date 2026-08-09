import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CandidateImports } from "./candidate-imports";
import type { CandidateImport } from "../candidates";

/**
 * The imports panel's polling contract: while a run is in flight the list is
 * re-read every few seconds, and a transient failure on one of those reads
 * must not leave an error banner sitting over a table that is visibly
 * refreshing. A successful poll is the answer to a failed one.
 */

function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  };
}

function row(overrides: Partial<CandidateImport> = {}): CandidateImport {
  return {
    id: "import-1",
    filename: "roster.csv",
    content_type: "text/csv",
    byte_size: 10,
    state: "parsing",
    candidates_created: 0,
    candidates_updated: 0,
    roles_created: 0,
    roles_updated: 0,
    rows_failed: 0,
    has_errors: false,
    created_at: null,
    ...overrides,
  };
}

const LIST_SUFFIX = "/api/candidates/imports?limit=8";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("CandidateImports", () => {
  it("clears a transient poll error once the list is readable again", async () => {
    vi.useFakeTimers();
    let calls = 0;
    const fetchMock = vi.fn(async (url: unknown) => {
      if (!String(url).endsWith(LIST_SUFFIX)) return jsonResponse({ detail: "no" }, { ok: false, status: 404 });
      calls += 1;
      // 1: a run in flight, so polling starts. 2: the poll fails. 3: the
      // next poll succeeds — the error from 2 must be gone.
      if (calls === 1) return jsonResponse([row()]);
      if (calls === 2) return jsonResponse({ detail: "the server hiccuped" }, { ok: false, status: 502 });
      return jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    const onImported = vi.fn();

    render(<CandidateImports onImported={onImported} onUndone={vi.fn()} />);

    // Initial read: the in-flight row lands, arming the poll.
    await act(async () => {});
    expect(screen.getByText("Reading the rows now.")).toBeTruthy();

    // First poll: the read fails, so an error appears.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText(/the server hiccuped/)).toBeTruthy();

    // Second poll: the read succeeds again. The stale error must clear —
    // that is the bug this test pins.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(screen.queryByRole("alert")).toBeNull();
    // The run finished, so the poll stops rather than asking forever.
    expect(onImported.mock.calls.length).toBeGreaterThanOrEqual(1);
  }, 10000);
});
