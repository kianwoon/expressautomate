import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../auth";
import { JobOrders } from "./job-orders";
import { resetMembers } from "./members";
import type { Opportunity } from "./opportunities";

/**
 * A claim has to stick — in the panel *and* in the row beside it.
 *
 * The panel is synced from the list on every render, so writing the read-back
 * row only into the panel is not enough: the effect finds the pre-claim object
 * still sitting in the page, sees a different identity, and pins the selection
 * back to it. The new owner appears for one tick and then reverts to
 * "Unassigned" — a claim that visibly undoes itself, which reads as the claim
 * having failed.
 *
 * So this file asserts *after* the sync effect has had its go, not merely after
 * the click, and it asserts on the list avatar too. Either half alone can be
 * made green while the other is wrong.
 *
 * `./events` is stubbed because the live stream opens an `EventSource`, which
 * jsdom does not have — and nothing here is about the wire. Nothing re-polls
 * the list either, which is the point: the list must be correct because the
 * claim put the row there, not because a refresh happened to arrive.
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

const CLAIMED = opportunity({ assigned_user_id: "u-1", assignee_name: "Priya Nair" });

afterEach(() => {
  cleanup();
  resetMembers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("claiming a job order from the dashboard", () => {
  it("leaves the panel and the list row both showing the new owner", async () => {
    // The list keeps answering with the *unclaimed* row, exactly as a page
    // fetched before the click does. Only the single-row read-back knows about
    // the claim, so the assertions below can only pass if that read-back
    // reaches the list as well as the panel.
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      const href = String(url);
      const json = href.includes("/auth/me")
        ? me()
        : href.endsWith("/claim")
          ? {}
          : href.includes("/api/opportunities/op-1")
            ? CLAIMED
            : href.includes("/api/opportunities")
              ? page([opportunity()])
              : href.includes("/members")
                ? []
                : {};
      void init;
      return { ok: true, status: 200, json: async () => json } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<JobOrders me={me()} />);

    const claim = await screen.findByRole("button", { name: "Claim this job order" });
    fireEvent.click(claim);

    // After the sync effect has run, not merely after the click. Against the
    // old code the panel showed the owner for one tick and then reverted.
    await waitFor(() =>
      expect(screen.getByTestId("jo-detail-owner").textContent).toContain("Priya Nair"),
    );
    // And it stays. A second settled pass catches a revert that lands a tick
    // late rather than in the same one.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Claim this job order" })).toBeNull());
    expect(screen.getByTestId("jo-detail-owner").textContent).not.toContain("Unassigned");

    // The row agrees. A correct panel beside a stale avatar is the lesser bug
    // and still a bug — the recruiter is looking at both at once.
    expect(screen.getAllByRole("img", { name: "Priya Nair" }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("img", { name: "Unassigned" })).toBeNull();
  });
});
