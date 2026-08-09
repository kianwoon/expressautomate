import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../auth";
import { JobOrders } from "./job-orders";
import { JobOrdersTable } from "./job-orders-table";
import { DEFAULT_SORT } from "./job-orders-table";
import type { Opportunity } from "./opportunities";

/**
 * Whose job order it is, made visible: a second chip row for ownership, and the
 * owner's face in the row itself.
 *
 * The two things pinned here are the two that are easy to lose later. First,
 * ownership and review state are independent axes — a recruiter asking for
 * "mine, needing review" is asking one question, not choosing between two, so
 * both parameters have to survive into the same request. Second, the avatar
 * lives inside the company cell: the table is `table-layout: fixed` and its
 * eight widths were fitted to their content, so a ninth column would silently
 * squeeze every one of them.
 *
 * `./events` is stubbed because the live stream opens an `EventSource`, which
 * jsdom does not have — and this file is about filters and cells, not about the
 * wire.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

// The detail panel's shortlist reads its own endpoint, and a `fetch` stubbed
// to answer with a page of job orders is not an answer it can make sense of.
// It has its own test file; this one is about the chips.
vi.mock("./job-orders-sourcing", () => ({ Shortlist: () => null }));

vi.mock("../events", () => ({
  useLive: () => {},
  useLiveStatus: () => "live",
}));

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-07-30T00:00:00Z",
    company_name_raw: "Acme",
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
      display_name: "Recruiter",
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

/** The last URL asked of the *list* endpoint. The dashboard also fetches the
 *  activity log and the mailbox overview, so the plain last call is not
 *  reliably the one under test. */
function lastUrl(fetchMock: ReturnType<typeof vi.fn>): string {
  const urls = fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("opportunities"));
  return urls[urls.length - 1] ?? "";
}

function table(rows: Opportunity[]) {
  return (
    <JobOrdersTable
      rows={rows}
      sort={DEFAULT_SORT}
      onSort={() => {}}
      selectedId={null}
      onSelect={() => {}}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("scope chips", () => {
  it("renders a second chip row that combines with the status chips", async () => {
    // Ownership and review state are independent axes. A recruiter wanting
    // "mine, needing review" should not have to choose which question to ask.
    // Answers `/auth/me` with a `Me` rather than with a page of job orders.
    // A blanket stub let `useAuth()` report signed-in while handing back an
    // object with no `.user`, a shape the app cannot produce — and the panel
    // had grown a defensive `?.` to survive it. A fixture that lies is worse
    // than no fixture: it buys hardening against nothing.
    const fetchMock = vi.fn().mockImplementation(
      async (url: string) =>
        ({
          ok: true,
          status: 200,
          json: async () => (String(url).includes("/auth/me") ? me() : page([opportunity()])),
        }) as Response,
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<JobOrders me={me()} />);
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("offset=0"));

    // Each chip is pressed inside its own group. The two rows no longer share
    // a label — "All job orders" and "Everyone" say which question they answer
    // — but the groups stay the anchor so a future label clash cannot make one
    // of these clicks land on the wrong row silently.
    const status = screen.getByRole("group", { name: "Filter job orders" });
    fireEvent.click(within(status).getByRole("button", { name: /Needs review/ }));
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("status=needs_review"));

    const owner = screen.getByRole("group", { name: "Filter job orders by owner" });
    fireEvent.click(within(owner).getByRole("button", { name: "Mine" }));

    await waitFor(() => expect(lastUrl(fetchMock)).toContain("scope=mine"));
    // The status filter is still asked for. Selecting an owner must not answer
    // the other question on the recruiter's behalf.
    expect(lastUrl(fetchMock)).toContain("status=needs_review");
  });
});

describe("the assignee avatar", () => {
  it("shows initials in the company cell for an assigned job order", async () => {
    render(table([opportunity({ assigned_user_id: "u-9", assignee_name: "Priya Nair" })]));

    // Announced as the person, not as two letters — `Initials` is `role="img"`.
    const avatar = screen.getByRole("img", { name: "Priya Nair" });
    expect(avatar.textContent).toBe("PN");
    // Inside the company cell, sharing it with the button that opens the row.
    const cell = screen.getByRole("button", { name: "Show details for Acme" }).closest("td");
    expect(cell?.contains(avatar)).toBe(true);
  });

  it("shows a dashed empty circle for a queue item", async () => {
    render(table([opportunity()]));

    // The unassigned queue is a real, visible state, not missing data — so it
    // is drawn, and it is drawn differently from a person.
    const empty = screen.getByRole("img", { name: "Unassigned" });
    expect(empty.className).toBe("jo-owner-empty");
    expect(screen.queryByRole("img", { name: "Acme" })).toBe(null);
  });

  it("puts the assignee name in the title so a hover names them", async () => {
    render(table([opportunity({ assigned_user_id: "u-9", assignee_name: "Priya Nair" })]));

    expect(screen.getByTitle("Priya Nair").className).toBe("jo-owner");
  });

  it("does not add an eighth column", async () => {
    // The table is table-layout:fixed and the columns were fitted to their
    // content — Location left the table, and the seven that remain still sum
    // to 100% in job-orders.css.
    render(table([opportunity({ assigned_user_id: "u-9", assignee_name: "Priya Nair" })]));

    expect(screen.getAllByRole("columnheader")).toHaveLength(7);
  });
});
