import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../../auth";
import type { Client } from "../clients";
import { resetMembers } from "../members";
import { ClientAssignee } from "./client-assignee";

/**
 * Which recruiter looks after which client.
 *
 * This is what drives routing: until a client has a recruiter, a job order
 * arriving by email lands on nobody's desk. Three rules are pinned here.
 *
 * The move checkbox starts ticked, because a client changing hands normally
 * means the work changes hands — and the server's count comes back so the
 * screen can say how much moved rather than moving a dozen job orders in
 * silence.
 *
 * The control is hidden in exactly the cases the API refuses with a 403: the
 * owner, the current assignee, or anyone at all when nobody holds the client.
 * The 403 test asserts twice — that the control is absent, and that no
 * assignee request went out. A permission check that only hid the button
 * after firing the request would still have moved the job orders.
 *
 * And collaborators say plainly that they grant nothing. Without that
 * sentence the list reads like a share, which it is not.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

let authState: Me | null = null;

vi.mock("../../auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../auth")>();
  return {
    ...actual,
    useAuth: () => (authState ? { status: "signed-in", me: authState } : { status: "loading" }),
  };
});

const STAFF = [
  { id: "u1", name: "Priya Nair", email: "priya@agency.sg", role: "member" },
  { id: "u2", name: "Sarah Lim", email: "sarah@agency.sg", role: "member" },
  { id: "u3", name: "Boss Tan", email: "boss@agency.sg", role: "owner" },
  { id: "u4", name: "Wei Ling", email: "wei@agency.sg", role: "member" },
];

function me(id: string, role: string): Me {
  return {
    user: { id, email: `${id}@agency.sg`, display_name: "Someone", preferred_name: null, role },
    tenant: { id: "t-1", name: "Agency", is_personal_account: false },
    mailbox: { provider: "microsoft", connected: true, scopes: [], status: "active" },
  } as unknown as Me;
}

function client(overrides: Partial<Client> = {}): Client {
  return {
    id: "cl-1",
    name: "Meridian Partners",
    name_normalized: "meridian partners",
    email_domain: "meridianpartners.com",
    status: "confirmed",
    merged_into_client_id: null,
    last_seen_at: null,
    created_at: "2026-07-30T00:00:00Z",
    website: null,
    phone: null,
    address: null,
    fee_percent: null,
    payment_terms_days: null,
    notes: null,
    source: "manual",
    suspended_reason: null,
    suspended_at: null,
    logo_key: null,
    logo_updated_at: null,
    assigned_user_id: null,
    assignee_name: null,
    collaborators: [],
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body, text: async () => "" };
}

/** Routes by URL: the staff list the picker reads, the assignee endpoint, and
 *  the collaborator endpoint are three different answers. */
function mockFetch(moved = 0) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.includes("/assignee")) {
      return jsonResponse({
        client_id: "cl-1",
        assigned_user_id: "u2",
        opportunities_moved: moved,
      });
    }
    if (url.includes("/collaborators")) {
      if (method === "DELETE") return jsonResponse(null, 204);
      // Idempotent server-side: the same colleague twice is a 201 both times.
      return jsonResponse({ client_id: "cl-1", user_id: "u4" }, 201);
    }
    return jsonResponse(STAFF);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The select stays disabled until the staff list has arrived. */
async function assigneeSelect(): Promise<HTMLSelectElement> {
  const select = screen.getByLabelText("Looked after by") as HTMLSelectElement;
  await waitFor(() => expect(select.disabled).toBe(false));
  return select;
}

function moveCheckbox(): HTMLInputElement {
  return screen.getByLabelText(/move this client's job orders/i) as HTMLInputElement;
}

/** The body of the last write of a given method. */
function lastBody(
  fetchMock: ReturnType<typeof mockFetch>,
  method: string,
): Record<string, unknown> {
  const write = [...fetchMock.mock.calls]
    .reverse()
    .find((call) => (call[1] as RequestInit | undefined)?.method === method);
  if (!write) throw new Error(`nothing was sent with ${method}`);
  return JSON.parse((write[1] as RequestInit).body as string) as Record<string, unknown>;
}

function assigneeCalls(fetchMock: ReturnType<typeof mockFetch>) {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes("/assignee"));
}

beforeEach(() => {
  authState = me("u1", "member");
  resetMembers();
});

afterEach(() => {
  cleanup();
  resetMembers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the client's recruiter", () => {
  it("defaults the move checkbox to on", async () => {
    // A client changing hands normally means the work changes hands.
    mockFetch();
    render(<ClientAssignee client={client({ assigned_user_id: "u1" })} onChanged={() => {}} />);

    await assigneeSelect();
    expect(moveCheckbox().checked).toBe(true);
  });

  it("reports how many job orders moved", async () => {
    // A reassignment that moves a dozen job orders silently is what the count
    // exists to prevent.
    mockFetch(12);
    render(<ClientAssignee client={client({ assigned_user_id: "u1" })} onChanged={() => {}} />);

    const select = await assigneeSelect();
    fireEvent.change(select, { target: { value: "u2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText(/12 job orders moved to Sarah/i)).not.toBeNull();
  });

  it("sends move_open_opportunities false when unticked", async () => {
    // The request field keeps the API's name even though no user-facing string
    // may say "open" — nothing in the schema makes that word true.
    const fetchMock = mockFetch(0);
    render(<ClientAssignee client={client({ assigned_user_id: "u1" })} onChanged={() => {}} />);

    const select = await assigneeSelect();
    fireEvent.click(moveCheckbox());
    fireEvent.change(select, { target: { value: "u2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(assigneeCalls(fetchMock).length).toBe(1));
    expect(lastBody(fetchMock, "PUT")).toEqual({
      user_id: "u2",
      move_open_opportunities: false,
    });
  });

  it("is hidden when the caller is neither the owner nor the assignee", async () => {
    // Mirrors the 403 the API returns — the interface should not offer an
    // action that will be refused.
    const fetchMock = mockFetch(12);
    authState = me("u4", "member");
    render(
      <ClientAssignee
        client={client({ assigned_user_id: "u2", assignee_name: "Sarah Lim" })}
        onChanged={() => {}}
      />,
    );

    // The fact is still stated; only the ability to change it is withheld.
    expect(await screen.findByText("Sarah Lim")).not.toBeNull();
    expect(screen.queryByLabelText("Looked after by")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();

    // And the second half of the rule: a check that hid the control only after
    // firing the request would already have moved this client's job orders.
    expect(assigneeCalls(fetchMock)).toEqual([]);
  });

  it("is offered on an unassigned client to any recruiter", async () => {
    // A client nobody holds is claimable, like the unassigned job-order queue.
    mockFetch();
    authState = me("u4", "member");
    render(<ClientAssignee client={client({ assigned_user_id: null })} onChanged={() => {}} />);

    expect(await assigneeSelect()).not.toBeNull();
  });

  it("says collaborators grant no access to the client's job orders", async () => {
    // Without that said plainly, the list reads like a share.
    mockFetch();
    render(<ClientAssignee client={client({ assigned_user_id: "u1" })} onChanged={() => {}} />);

    expect(
      await screen.findByText(/grants no access to this client's job orders/i),
    ).not.toBeNull();
  });

  it("adding the same collaborator twice does not error", async () => {
    const fetchMock = mockFetch();
    render(<ClientAssignee client={client({ assigned_user_id: "u1" })} onChanged={() => {}} />);

    const picker = screen.getByLabelText(
      "Add a colleague who also knows this account",
    ) as HTMLSelectElement;
    await waitFor(() => expect(picker.disabled).toBe(false));

    for (let i = 0; i < 2; i += 1) {
      fireEvent.change(picker, { target: { value: "u4" } });
      fireEvent.click(screen.getByRole("button", { name: "Add" }));
      await waitFor(() =>
        expect(screen.queryAllByRole("button", { name: "Remove Wei Ling" }).length).toBe(1),
      );
    }

    // Idempotent, not an error: nothing is shouted at the recruiter for
    // naming a colleague who was already named.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes("/collaborators")).length,
    ).toBe(2);
  });

  it("offers the caller in the assignee picker but not the collaborator picker", async () => {
    // Naming yourself as assignee is the common case (taking on a client);
    // naming yourself as a collaborator says nothing — you are already the
    // assignee or an owner looking at this panel.
    mockFetch();
    render(<ClientAssignee client={client({ assigned_user_id: "u1" })} onChanged={() => {}} />);

    const assignee = await assigneeSelect();
    const collaboratorPicker = screen.getByLabelText(
      "Add a colleague who also knows this account",
    ) as HTMLSelectElement;
    await waitFor(() => expect(collaboratorPicker.disabled).toBe(false));

    const assigneeOptionValues = [...assignee.options].map((o) => o.value);
    const collaboratorOptionValues = [...collaboratorPicker.options].map((o) => o.value);

    expect(assigneeOptionValues).toContain("u1");
    expect(collaboratorOptionValues).not.toContain("u1");
  });
});
