import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClientSearch } from "./client-search";

/**
 * The one client search, tested once.
 *
 * Two places need to point a job order at a client — the manual form and the
 * detail panel — and the behaviour they share is here rather than twice.
 *
 * The debounce test is the reason this file exists at all. The manual form's
 * own test asserts only that a search happened, which is equally true of an
 * implementation that fires a request per keystroke. This one counts.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

/** Long enough that a debounce of 300ms has fired and a per-keystroke
 *  implementation has fired three times. */
const SETTLE_MS = 600;

function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  };
}

function mockSearch(items: Array<{ id: string; name: string }> = [{ id: "cl-1", name: "Sunrise Logistics" }]) {
  const fetchMock = vi.fn(async () => jsonResponse({ items, total: items.length }));
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
  return fetchMock;
}

type AnyMock = ReturnType<typeof vi.fn>;

function searchCalls(fetchMock: AnyMock): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("/api/clients"));
}

function lastUrl(fetchMock: AnyMock): string {
  const urls = searchCalls(fetchMock);
  return urls[urls.length - 1] ?? "";
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ClientSearch", () => {
  it("searches as you type rather than preloading", async () => {
    // Clients are paginated and an agency accumulates hundreds; this is why it
    // is a search and not a dropdown.
    const fetchMock = mockSearch();
    render(<ClientSearch value={null} onChange={() => {}} label="Client" />);

    // Nothing is fetched before a letter is typed — a dropdown would have.
    expect(searchCalls(fetchMock)).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("Client"), { target: { value: "sun" } });
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("q=sun"));
  });

  it("debounces rather than firing per keystroke", async () => {
    // Type three characters in quick succession; only one request should go out.
    // The existing test for this behaviour passes equally with one request per
    // keystroke, which is why this one counts calls.
    const fetchMock = mockSearch();
    render(<ClientSearch value={null} onChange={() => {}} label="Client" />);
    const input = screen.getByLabelText("Client");

    fireEvent.change(input, { target: { value: "s" } });
    fireEvent.change(input, { target: { value: "su" } });
    fireEvent.change(input, { target: { value: "sun" } });

    await waitFor(() => expect(searchCalls(fetchMock).length).toBeGreaterThan(0));
    await sleep(SETTLE_MS);

    expect(searchCalls(fetchMock)).toHaveLength(1);
    // And the one request carries the whole word, not the first letter.
    expect(lastUrl(fetchMock)).toContain("q=sun");
  });

  it("reports the chosen client", async () => {
    const fetchMock = mockSearch();
    const onChange = vi.fn();
    render(<ClientSearch value={null} onChange={onChange} label="Client" />);

    fireEvent.change(screen.getByLabelText("Client"), { target: { value: "sun" } });
    await waitFor(() => expect(searchCalls(fetchMock).length).toBeGreaterThan(0));

    const option = await screen.findByRole("option", { name: "Sunrise Logistics" });
    fireEvent.click(option);

    expect(onChange).toHaveBeenCalledWith({ id: "cl-1", name: "Sunrise Logistics" });
  });

  it("clears back to no client", async () => {
    // A job order taken over the phone from an unrecorded company legitimately
    // has none.
    mockSearch();
    const onChange = vi.fn();
    render(
      <ClientSearch value={{ id: "cl-1", name: "Sunrise Logistics" }} onChange={onChange} label="Client" />,
    );

    const input = screen.getByLabelText("Client") as HTMLInputElement;
    expect(input.value).toBe("Sunrise Logistics");

    // Editing the text has visibly typed away from the client that was chosen,
    // so the id must not travel with it.
    fireEvent.change(input, { target: { value: "Sunrise Logistic" } });

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("reports the raw typed text as it is typed, unmatched or not", async () => {
    const fetchMock = mockSearch([]);
    const onQueryChange = vi.fn();
    render(
      <ClientSearch value={null} onChange={() => {}} label="Client" onQueryChange={onQueryChange} />,
    );

    fireEvent.change(screen.getByLabelText("Client"), { target: { value: "Nobody Yet Pte Ltd" } });
    expect(onQueryChange).toHaveBeenCalledWith("Nobody Yet Pte Ltd");
    await waitFor(() => expect(searchCalls(fetchMock).length).toBeGreaterThan(0));
  });

  it("reports the matched name too when a match is picked", async () => {
    mockSearch();
    const onChange = vi.fn();
    const onQueryChange = vi.fn();
    render(
      <ClientSearch value={null} onChange={onChange} label="Client" onQueryChange={onQueryChange} />,
    );

    fireEvent.change(screen.getByLabelText("Client"), { target: { value: "sun" } });
    const option = await screen.findByRole("option", { name: "Sunrise Logistics" });
    fireEvent.click(option);

    expect(onChange).toHaveBeenCalledWith({ id: "cl-1", name: "Sunrise Logistics" });
    expect(onQueryChange).toHaveBeenCalledWith("Sunrise Logistics");
  });

  it("notes that an unmatched name becomes a new client on save", async () => {
    mockSearch([]);
    render(<ClientSearch value={null} onChange={() => {}} label="Client" />);

    fireEvent.change(screen.getByLabelText("Client"), { target: { value: "Nobody Yet Pte Ltd" } });

    const note = await screen.findByRole("status");
    expect(note.textContent).toContain("will be added as a new client");
  });

  it("never shows the new-client note for a term that turns out to match — not mid-flight, not after", async () => {
    // A response the test controls the timing of, so it can inspect the DOM
    // both while the request is still in flight and after it lands. A
    // fetch mock that resolves immediately (like the others in this file)
    // cannot catch a flag that is wrongly true for that brief window.
    let resolveFetch: (value: unknown) => void = () => {};
    const fetchMock = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    render(<ClientSearch value={null} onChange={() => {}} label="Client" />);
    fireEvent.change(screen.getByLabelText("Client"), { target: { value: "sun" } });

    // Debounce has elapsed and the request is in flight, unresolved.
    await sleep(SETTLE_MS);
    expect(screen.queryByText(/will be added as a new client/)).toBeNull();

    // It resolves with a match — "sun" was never a name nobody has recorded.
    resolveFetch(jsonResponse({ items: [{ id: "cl-1", name: "Sunrise Logistics" }] }));
    await screen.findByRole("option", { name: "Sunrise Logistics" });

    expect(screen.queryByText(/will be added as a new client/)).toBeNull();
  });

  it("takes a placeholder", () => {
    mockSearch();
    render(<ClientSearch value={null} onChange={() => {}} label="Client" placeholder="Acme Pte Ltd" />);
    expect(screen.getByLabelText("Client").getAttribute("placeholder")).toBe("Acme Pte Ltd");
  });

  it("says so when the search cannot be read", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ detail: "boom" }, { ok: false, status: 500 }));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    render(<ClientSearch value={null} onChange={() => {}} label="Client" />);

    fireEvent.change(screen.getByLabelText("Client"), { target: { value: "sun" } });

    const note = await screen.findByRole("status");
    expect(note.textContent).toContain("Could not search clients");
    // Quiet, not blocking: the field is optional and the rest of the screen is
    // fine, so there is no alert and no list of stale matches.
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
