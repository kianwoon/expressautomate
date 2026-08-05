import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../auth";
import { JobOrders } from "./job-orders";
import { resetMembers } from "./members";
import type { Opportunity } from "./opportunities";

/**
 * Linking a job order to its client, from the panel a recruiter actually reads.
 *
 * Rendered through `JobOrders` rather than through `DetailPanel` alone, and
 * that is the whole point of the file: the panel does not own the write. The
 * request body, the read-back, the list row and the panel are four things that
 * have to agree, and a panel-only render can be made green while the row
 * beside it still shows the old owner — the exact bug the claim path shipped
 * once already.
 *
 * `./job-orders-sourcing` and `../events` are stubbed for the same reasons as
 * in `job-orders-claim.test.tsx`: the shortlist reads its own endpoint and has
 * its own file, and `EventSource` does not exist in jsdom. Nothing here re-polls
 * the list, so the list can only be right because the write put the row there.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

vi.mock("./job-orders-sourcing", () => ({ Shortlist: () => null }));

vi.mock("../events", () => ({
  useLive: () => {},
  useLiveStatus: () => "live",
}));

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-07-30T00:00:00Z",
    company_name_raw: "Sunrise Care Pte Ltd",
    job_title_raw: "Accountant",
    salary_raw: null,
    salary_min: null,
    salary_max: null,
    salary_currency: null,
    salary_period: null,
    working_hours_raw: null,
    requirements: null,
    job_description: null,
    duration_raw: null,
    location_raw: null,
    quality_state: "verified",
    review_status: "new",
    internet_message_id: null,
    graph_message_id: null,
    verified_fields: 0,
    total_fields: 0,
    placement_type: null,
    sex_requirement: null,
    sex_requirement_reason: null,
    assigned_user_id: null,
    assignee_name: null,
    client_id: null,
    client_name: null,
    source: "pipeline",
    shared_with_me: false,
    ...overrides,
  } as Opportunity;
}

function me(): Me {
  return {
    user: {
      id: "u-1",
      email: "recruiter@agency.sg",
      display_name: "Priya Nair",
      preferred_name: null,
      role: "member",
    },
    tenant: { id: "t-1", name: "Agency", is_personal_account: false },
    mailbox: {
      provider: "microsoft",
      connected: true,
      awaiting_period: false,
      scopes: [],
      status: "active",
      ingestion_active: true,
      ingested: {
        total: 1,
        in_progress: 0,
        awaiting_extraction: 0,
        emails_extracted: 1,
        opportunities: 1,
      },
      oldest_received: null,
      newest_received: null,
      last_activity: null,
    },
  } as Me;
}

function page(items: Opportunity[]) {
  return {
    items,
    total: items.length,
    limit: 20,
    offset: 0,
    counts: { all: items.length, new: items.length, needs_review: 0, reviewed: 0 },
  };
}

const MATCHES = { items: [{ id: "c-1", name: "Sunrise Care Pte Ltd" }] };

/** The row as it comes back once the link has been made: the client is set,
 *  and the client's own recruiter has taken it on. */
const LINKED = opportunity({
  client_id: "c-1",
  client_name: "Sunrise Care Pte Ltd",
  assigned_user_id: "u-2",
  assignee_name: "Wei Kian",
});

type FetchMock = ReturnType<typeof vi.fn>;

/**
 * Wires one screen up.
 *
 * `clientStatus` is the status the link POST answers with, which is the only
 * axis these tests vary. Everything else answers the same way every time: the
 * *list* keeps returning the unlinked row, exactly as a page fetched before
 * the click does, so only the single-row read-back knows about the link.
 */
function mount(
  options: { row?: Opportunity; clientStatus?: number; readBackFails?: boolean } = {},
): FetchMock {
  const row = options.row ?? opportunity();
  const clientStatus = options.clientStatus ?? 200;
  const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    void init;
    const href = String(url);
    if (href.endsWith("/client")) {
      return {
        ok: clientStatus < 400,
        status: clientStatus,
        json: async () => ({
          id: "op-1",
          client_id: "c-1",
          client_name: "Sunrise Care Pte Ltd",
          assigned_user_id: "u-2",
          assignee_name: "Wei Kian",
        }),
      } as Response;
    }
    // The single-row read-back refusing, so that whatever the screen still
    // shows can only have come from the link response itself.
    if (options.readBackFails && href.includes("/api/opportunities/op-1")) {
      return { ok: false, status: 500, json: async () => ({}) } as Response;
    }
    const json = href.includes("/auth/me")
      ? me()
      : href.includes("/api/clients")
        ? MATCHES
        : href.includes("/api/opportunities/op-1")
          ? LINKED
          : href.includes("/api/opportunities")
            ? page([row])
            : href.includes("/members")
              ? []
              : {};
    return { ok: true, status: 200, json: async () => json } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<JobOrders me={me()} />);
  return fetchMock;
}

/** The body of the last link POST, parsed. */
function lastBody(fetchMock: FetchMock): unknown {
  const calls = fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/client"));
  const init = calls[calls.length - 1]?.[1] as RequestInit | undefined;
  return JSON.parse(String(init?.body));
}

/** Types a company name, waits for the match, and picks it. */
async function chooseClient(): Promise<void> {
  const input = await screen.findByLabelText("Client");
  fireEvent.change(input, { target: { value: "sunrise" } });
  const option = await screen.findByRole("option", { name: "Sunrise Care Pte Ltd" });
  fireEvent.click(option);
}

/** Sends it. */
function link(): void {
  fireEvent.click(screen.getByRole("button", { name: "Link this client" }));
}

/** Opens the detail modal by clicking the row's accessible "show details" button.
 *
 * The detail is a modal now, not an always-open panel, so nothing in it is on
 * the page until a row is opened. Every test below asserts on detail content,
 * so every one opens the row first. */
async function openDetail(): Promise<void> {
  fireEvent.click(
    await screen.findByRole("button", { name: /Show details for Sunrise Care Pte Ltd/i }),
  );
}

afterEach(() => {
  cleanup();
  resetMembers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("linking a job order to its client", () => {
  it("says a job order is not linked to a client", async () => {
    // The company name from the AI extraction sits directly above this field,
    // so a blank field alone implies the opposite of the truth.
    mount();
    await openDetail();
    expect(await screen.findByText(/not linked to a client/i)).toBeTruthy();
  });

  it("does not say that when a client is linked", async () => {
    mount({ row: opportunity({ client_id: "c-1", client_name: "Sunrise Care Pte Ltd" }) });
    await openDetail();
    await screen.findByLabelText("Client");
    expect(screen.queryByText(/not linked to a client/i)).toBeNull();
  });

  it("shows the client an already-linked job order is filed under", async () => {
    // Without this the picker is empty on a linked row and on an unlinked one
    // alike, so the only way to check what was linked is the absence of a
    // sentence — which is not a way to audit eight rows.
    mount({ row: opportunity({ client_id: "c-1", client_name: "Sunrise Care Pte Ltd" }) });
    await openDetail();
    const input = (await screen.findByLabelText("Client")) as HTMLInputElement;
    expect(input.value).toBe("Sunrise Care Pte Ltd");
  });

  it("leaves the picker empty on an unlinked job order", async () => {
    mount();
    await openDetail();
    const input = (await screen.findByLabelText("Client")) as HTMLInputElement;
    expect(input.value).toBe("");
    expect(screen.getByText(/not linked to a client/i)).toBeTruthy();
  });

  it("keeps showing the client after linking, without reading the row back", async () => {
    // The link response names the client, so the panel can show what it just
    // did even when the read-back fails.
    mount({ readBackFails: true });
    await openDetail();
    await chooseClient();
    link();

    await waitFor(() =>
      expect(screen.queryByText(/not linked to a client/i)).toBeNull(),
    );
    const input = screen.getByLabelText("Client") as HTMLInputElement;
    expect(input.value).toBe("Sunrise Care Pte Ltd");
  });

  it("posts the chosen client with adopt defaulted on", async () => {
    const fetchMock = mount();
    await openDetail();
    await chooseClient();
    link();

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/client"))).toBe(true),
    );
    expect(lastBody(fetchMock)).toEqual({
      client_id: "c-1",
      adopt_client_recruiter: true,
    });
  });

  it("sends adopt false when the checkbox is cleared", async () => {
    const fetchMock = mount();
    await openDetail();
    await chooseClient();
    fireEvent.click(screen.getByLabelText("Also take on this client\u2019s recruiter"));
    link();

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/client"))).toBe(true),
    );
    expect(lastBody(fetchMock)).toEqual({
      client_id: "c-1",
      adopt_client_recruiter: false,
    });
  });

  it("shows who the job order went to after linking", async () => {
    // The response names the recruiter so ownership does not change silently.
    mount();
    await openDetail();
    await chooseClient();
    link();

    await waitFor(() =>
      expect(screen.getByTestId("jo-detail-owner").textContent).toContain("Wei Kian"),
    );
    // And it stays: the sync effect gets a second settled pass at it.
    await waitFor(() =>
      expect(screen.getByTestId("jo-detail-owner").textContent).not.toContain("Unassigned"),
    );
  });

  it("updates the list row too, not just the panel", async () => {
    // `patchRow` exists for exactly this; without it the list shows a stale
    // owner beside a correct panel.
    mount();
    await openDetail();
    await chooseClient();
    link();

    await waitFor(() =>
      expect(screen.getAllByRole("img", { name: "Wei Kian" }).length).toBeGreaterThan(0),
    );
    expect(screen.queryByRole("img", { name: "Unassigned" })).toBeNull();
  });

  it("renders a 403 as the shared-not-assigned sentence", async () => {
    mount({
      row: opportunity({
        assigned_user_id: "u-2",
        assignee_name: "Wei Kian",
        shared_with_me: true,
      }),
      clientStatus: 403,
    });
    await openDetail();
    await chooseClient();
    link();

    expect((await screen.findByRole("alert")).textContent).toContain(
      "This job order is shared with you, not assigned to you.",
    );
  });

  it("closes the modal on a 404", async () => {
    // A 404 is a close, not an error: the detail reports the id upward and the
    // parent clears the selection, unmounting the modal. Nothing of the detail
    // is left on the page — not the owner line, and not a stale message.
    mount({ clientStatus: 404 });
    await openDetail();
    await chooseClient();
    link();

    await waitFor(() =>
      expect(screen.queryByText("This job order is no longer available.")).toBeNull(),
    );
    expect(screen.queryByTestId("jo-detail-owner")).toBeNull();
  });
});
