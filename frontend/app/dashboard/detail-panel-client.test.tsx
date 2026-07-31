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
function mount(options: { row?: Opportunity; clientStatus?: number } = {}): FetchMock {
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
          assigned_user_id: "u-2",
          assignee_name: "Wei Kian",
        }),
      } as Response;
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
    expect(await screen.findByText(/not linked to a client/i)).toBeTruthy();
  });

  it("does not say that when a client is linked", async () => {
    mount({ row: opportunity({ client_id: "c-1" }) });
    await screen.findByLabelText("Client");
    expect(screen.queryByText(/not linked to a client/i)).toBeNull();
  });

  it("posts the chosen client with adopt defaulted on", async () => {
    const fetchMock = mount();
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
    await chooseClient();
    link();

    expect((await screen.findByRole("alert")).textContent).toContain(
      "This job order is shared with you, not assigned to you.",
    );
  });

  it("closes the panel on a 404", async () => {
    mount({ clientStatus: 404 });
    await chooseClient();
    link();

    expect(await screen.findByText("This job order is no longer available.")).toBeTruthy();
    expect(screen.queryByTestId("jo-detail-owner")).toBeNull();
  });
});
