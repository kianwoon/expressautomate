import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { resetMembers, useMembers } from "./members";

function jsonResponse(body: unknown, init?: { status?: number }): Response {
  return {
    ok: (init?.status ?? 200) < 300,
    status: init?.status ?? 200,
    json: async () => body,
  } as Response;
}

beforeEach(() => {
  resetMembers();
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetMembers();
});

it("fetches once across two mounts", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValue(
      jsonResponse([{ id: "1", name: "Priya Nair", email: "p@a.sg", role: "recruiter" }]),
    );
  vi.stubGlobal("fetch", fetchMock);

  const first = renderHook(() => useMembers());
  await waitFor(() => expect(first.result.current.status).toBe("ready"));
  expect(first.result.current.members[0].name).toBe("Priya Nair");
  first.unmount();

  const second = renderHook(() => useMembers());
  await waitFor(() => expect(second.result.current.status).toBe("ready"));
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("reports unreadable when the request fails", async () => {
  const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}, { status: 500 }));
  vi.stubGlobal("fetch", fetchMock);

  const { result } = renderHook(() => useMembers());
  await waitFor(() => expect(result.current.status).toBe("unreadable"));
  expect(result.current.members).toEqual([]);
  expect(result.current.message).not.toBe(undefined);
});

it("does not cache a failure, so the next mount tries again", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(jsonResponse({}, { status: 500 }))
    .mockResolvedValue(
      jsonResponse([{ id: "1", name: "Priya Nair", email: "p@a.sg", role: "recruiter" }]),
    );
  vi.stubGlobal("fetch", fetchMock);

  const first = renderHook(() => useMembers());
  await waitFor(() => expect(first.result.current.status).toBe("unreadable"));
  first.unmount();

  const second = renderHook(() => useMembers());
  await waitFor(() => expect(second.result.current.status).toBe("ready"));
  expect(fetchMock).toHaveBeenCalledTimes(2);
});
