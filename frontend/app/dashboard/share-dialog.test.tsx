import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../auth";
import { resetMembers } from "./members";
import type { Opportunity } from "./opportunities";
import { ShareDialog } from "./share-dialog";

/**
 * Handing a job order to whoever can fill it.
 *
 * The rules being pinned here are the whole feature. A share grants sight and
 * nothing else, so there is no access level to pick. Seeing a job order is the
 * right to pass it on to a named colleague — that is how work finds the right
 * desk through a chain of people who each knew one more person. But throwing
 * someone else's client work open to the entire office is not a recipient's
 * decision, so the broadcast is the assignee's and the owner's alone, and on
 * an unassigned row there is no assignee at all: only an owner may broadcast
 * it, which is the shipped API behaviour rather than a rule invented here.
 *
 * That control is disabled with its reason showing rather than hidden. A
 * control that vanishes teaches nothing, and this is a rule worth learning
 * once instead of rediscovering by having a share refused.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

let authState: Me | null = null;

vi.mock("../auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../auth")>();
  return {
    ...actual,
    useAuth: () => (authState ? { status: "signed-in", me: authState } : { status: "loading" }),
  };
});

const STAFF = [
  { id: "u1", name: "Priya Nair", email: "priya@agency.sg", role: "member" },
  { id: "u2", name: "Wei Ling", email: "wei@agency.sg", role: "member" },
  { id: "u3", name: "Boss Tan", email: "boss@agency.sg", role: "owner" },
];

function me(id: string, role: string): Me {
  return {
    user: { id, email: `${id}@agency.sg`, display_name: "Someone", preferred_name: null, role },
    tenant: { id: "t-1", name: "Agency", is_personal_account: false },
    mailbox: { provider: "microsoft", connected: true, scopes: [], status: "active" },
  } as unknown as Me;
}

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    job_title_raw: "Care assistant",
    company_name_raw: "Acme",
    assigned_user_id: null,
    assignee_name: null,
    shared_with_me: false,
    ...overrides,
  } as Opportunity;
}

type ShareRow = {
  id: string;
  scope: string;
  shared_with_user_id: string | null;
  shared_by_user_id: string | null;
  note: string | null;
  created_at: string;
};

let shareRows: ShareRow[] = [];

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body, text: async () => "" };
}

/** Routes by URL, because two endpoints are in play: the staff list the
 *  picker reads and the shares this dialog writes. */
function mockFetch() {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.includes("/shares")) {
      if (method === "GET") return jsonResponse({ items: shareRows });
      if (method === "DELETE") return jsonResponse(null, 204);
      return jsonResponse({ opportunity_id: "op-1", newly_shared_with: 1 }, 201);
    }
    return jsonResponse(STAFF);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The body of the last write. */
function lastBody(fetchMock: ReturnType<typeof mockFetch>): Record<string, unknown> {
  const write = [...fetchMock.mock.calls]
    .reverse()
    .find((call) => (call[1] as RequestInit | undefined)?.method === "POST");
  if (!write) throw new Error("nothing was posted");
  return JSON.parse((write[1] as RequestInit).body as string) as Record<string, unknown>;
}

function open(row: Opportunity, onClose = () => {}) {
  return render(<ShareDialog row={row} onClose={onClose} />);
}

/** The picker only lists anyone once it has focus. */
async function pick(name: string) {
  // The picker stays disabled until it knows who the agency is.
  const input = screen.getByRole("combobox") as HTMLInputElement;
  await waitFor(() => expect(input.disabled).toBe(false));
  fireEvent.focus(input);
  const option = await screen.findByText(name);
  fireEvent.mouseDown(option);
}

function shareButton(): HTMLElement {
  return screen.getByRole("button", { name: "Share" });
}

beforeEach(() => {
  authState = me("u1", "member");
  shareRows = [];
  resetMembers();
});

afterEach(() => {
  cleanup();
  resetMembers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("sharing a job order", () => {
  it("posts the picked colleagues", async () => {
    const fetchMock = mockFetch();
    open(opportunity({ assigned_user_id: "u1", assignee_name: "Priya Nair" }));

    await pick("Wei Ling");
    fireEvent.click(shareButton());

    await waitFor(() => expect(lastBody(fetchMock)).toBeTruthy());
    expect(lastBody(fetchMock)).toEqual({ scope: "user", user_ids: ["u2"], note: null });
  });

  it("posts a tenant broadcast when the checkbox is ticked", async () => {
    const fetchMock = mockFetch();
    open(opportunity({ assigned_user_id: "u1", assignee_name: "Priya Nair" }));

    fireEvent.click(await screen.findByLabelText(/whole agency/i));
    fireEvent.click(shareButton());

    await waitFor(() => expect(lastBody(fetchMock)).toBeTruthy());
    expect(lastBody(fetchMock)).toEqual({ scope: "tenant", user_ids: [], note: null });
  });

  it("disables broadcast for a share recipient and says why", async () => {
    // A recipient may pass a job order to a named colleague but not throw
    // someone else's client work open to the office.
    mockFetch();
    open(opportunity({ assigned_user_id: "u2", assignee_name: "Wei Ling", shared_with_me: true }));

    const box = (await screen.findByLabelText(/whole agency/i)) as HTMLInputElement;
    expect(box.disabled).toBe(true);
    expect(screen.queryByText(/only the assigned recruiter/i)).not.toBeNull();
    // The named share is still theirs to make — sight is the right to pass on.
    expect(screen.getByRole("combobox")).toBeTruthy();
  });

  it("disables broadcast on an unassigned job order for a non-owner", async () => {
    // The API gates on can_edit, which refuses unassigned rows, falling back to
    // the owner role.
    mockFetch();
    open(opportunity());

    const box = (await screen.findByLabelText(/whole agency/i)) as HTMLInputElement;
    expect(box.disabled).toBe(true);
    expect(screen.queryByText(/claim it first/i)).not.toBeNull();
  });

  it("allows an owner-role user to broadcast an unassigned job order", async () => {
    authState = me("u3", "owner");
    const fetchMock = mockFetch();
    open(opportunity());

    const box = (await screen.findByLabelText(/whole agency/i)) as HTMLInputElement;
    expect(box.disabled).toBe(false);
    fireEvent.click(box);
    fireEvent.click(shareButton());

    await waitFor(() => expect(lastBody(fetchMock)).toBeTruthy());
    expect(lastBody(fetchMock)).toEqual({ scope: "tenant", user_ids: [], note: null });
  });

  it("lists who it is already shared with, and removes one", async () => {
    shareRows = [
      {
        id: "s-1",
        scope: "user",
        shared_with_user_id: "u2",
        shared_by_user_id: "u1",
        note: null,
        created_at: "2026-07-30T00:00:00Z",
      },
    ];
    const fetchMock = mockFetch();
    open(opportunity({ assigned_user_id: "u1", assignee_name: "Priya Nair" }));

    expect(await screen.findByText("Wei Ling")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Remove Wei Ling" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          (call) =>
            (call[1] as RequestInit | undefined)?.method === "DELETE" &&
            String(call[0]).includes("/shares/s-1"),
        ),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Remove Wei Ling" })).toBeNull(),
    );
  });

  it("says removal revokes sight, not their work", async () => {
    mockFetch();
    open(opportunity({ assigned_user_id: "u1", assignee_name: "Priya Nair" }));

    expect(await screen.findByText(/stops them seeing it/i)).toBeTruthy();
    expect(screen.getByText(/nothing they have done/i)).toBeTruthy();
  });

  it("hides people it is already shared with from the picker", async () => {
    shareRows = [
      {
        id: "s-1",
        scope: "user",
        shared_with_user_id: "u2",
        shared_by_user_id: "u1",
        note: null,
        created_at: "2026-07-30T00:00:00Z",
      },
    ];
    mockFetch();
    open(opportunity({ assigned_user_id: "u1", assignee_name: "Priya Nair" }));

    // Present as a recipient already; offering them again would post a share
    // that changes nothing.
    await screen.findByRole("button", { name: "Remove Wei Ling" });
    fireEvent.focus(screen.getByRole("combobox"));
    expect(await screen.findByText("Boss Tan")).toBeTruthy();
    expect(screen.queryByRole("option", { name: /Wei Ling/ })).toBeNull();
  });

  it("sends no note field when the note is blank", async () => {
    const fetchMock = mockFetch();
    open(opportunity({ assigned_user_id: "u1", assignee_name: "Priya Nair" }));

    await pick("Wei Ling");
    fireEvent.change(screen.getByLabelText(/note/i), { target: { value: "   " } });
    fireEvent.click(shareButton());

    await waitFor(() => expect(lastBody(fetchMock)).toBeTruthy());
    expect(lastBody(fetchMock).note).toBeNull();
  });
});
