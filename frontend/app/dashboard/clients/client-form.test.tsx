import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Client } from "../clients";
import { ClientForm } from "./client-form";

/**
 * The two ways this form can quietly destroy a record, pinned.
 *
 * 1. `PATCH /clients/{id}` is `exclude_unset`. A body carrying every field
 *    would turn each untouched empty input into an explicit `null` and wipe
 *    facts the recruiter never looked at — a fee, an address, a phone number
 *    gone because someone fixed a typo in the name. Only what changed may be
 *    sent, and that is the first test below.
 *
 * 2. A free email domain is a 422, and FastAPI's validation body puts the
 *    server's sentence in a *list* of `{loc, msg}` entries rather than in a
 *    string. Read naively that reaches the recruiter as `[object Object]`.
 *    The second test pins that the sentence itself arrives, against the
 *    field it is about.
 *
 * allow-hardcode: the strings below are test fixtures — a made-up client and
 * two verbatim copies of sentences the server writes. They are the input to
 * an assertion, not a list anything is matched against at runtime.
 */

function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  } as Response;
}

function client(overrides: Partial<Client> = {}): Client {
  return {
    id: "cl-1",
    name: "Acme Pte Ltd",
    name_normalized: "acme pte ltd",
    email_domain: "acme.com",
    status: "confirmed",
    merged_into_client_id: null,
    last_seen_at: null,
    created_at: "2026-07-30T00:00:00Z",
    website: "https://acme.com",
    phone: "+65 6555 0000",
    address: "1 Raffles Place",
    fee_percent: 18,
    payment_terms_days: 30,
    notes: "Pays on time.",
    source: "manual",
    suspended_reason: null,
    suspended_at: null,
    logo_key: null,
    logo_updated_at: null,
    mentions: [],
    contacts: [],
    ...overrides,
  };
}

function lastBody(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown> {
  const calls = fetchMock.mock.calls;
  return JSON.parse((calls[calls.length - 1][1] as RequestInit).body as string);
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ClientForm", () => {
  it("PATCHes only the field that changed", async () => {
    const row = client();
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ...row, fee_percent: 20 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<ClientForm client={row} onDone={() => {}} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText("Fee percent"), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // Exactly one key. Not `address: null`, not `notes: null` — those were
    // never touched and the server must go on holding them.
    expect(lastBody(fetchMock)).toEqual({ fee_percent: 20 });
  });

  it("shows a 422's own sentence against the domain field", async () => {
    const sentence =
      "gmail.com is a free email provider and identifies a person, not a company; leave the domain unset";
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        { detail: [{ loc: ["body", "email_domain"], msg: `Value error, ${sentence}` }] },
        { ok: false, status: 422 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<ClientForm client={null} onDone={() => {}} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText("Client name *"), { target: { value: "Sole Trader" } });
    fireEvent.change(screen.getByLabelText("Mail domain"), { target: { value: "gmail.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Add client" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(sentence);
    // Against the input it is about, so the recruiter knows what to change.
    // The label match is a prefix, not the whole name: the message renders
    // inside the label, which is what makes it part of the field's accessible
    // name rather than a sentence floating near it.
    expect(screen.getByLabelText(/^Mail domain/).getAttribute("aria-invalid")).toBe("true");
  });

  it("shows a 409's own sentence, which names the other client", async () => {
    const detail =
      "The domain acme.com is already used by another client. Merge the two, or leave this one without a domain.";
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail }, { ok: false, status: 409 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<ClientForm client={null} onDone={() => {}} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText("Client name *"), { target: { value: "Acme Two" } });
    fireEvent.change(screen.getByLabelText("Mail domain"), { target: { value: "acme.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Add client" }));

    expect((await screen.findByRole("alert")).textContent).toBe(detail);
  });

  it("reports the created id on Cancel after a partial create failure", async () => {
    // The client POST succeeds, then the contact call that follows it fails.
    // The client is real on the server from that moment on — Cancel must not
    // look like nothing happened, or the recruiter is liable to add it again.
    const created = client({ id: "cl-new" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(created))
      .mockResolvedValueOnce(
        jsonResponse({ detail: "could not save contact" }, { ok: false, status: 500 }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const onCancel = vi.fn();
    render(<ClientForm client={null} onDone={() => {}} onCancel={onCancel} />);

    fireEvent.change(screen.getByLabelText("Client name *"), { target: { value: "New Co" } });
    fireEvent.click(screen.getByRole("button", { name: "Add contact" }));
    fireEvent.change(screen.getAllByLabelText("Name *")[0], { target: { value: "Jo Tan" } });
    fireEvent.click(screen.getByRole("button", { name: "Add client" }));

    // The partial-failure banner, pinned already by the surrounding suite's
    // convention of asserting on the server-derived text.
    await screen.findByRole("alert");

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledWith("cl-new");
  });
});
