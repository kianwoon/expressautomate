import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Client } from "./clients";
import { Shortlist } from "./job-orders-sourcing";
import type { Opportunity } from "./opportunities";
import type { SourcingRun, SourcingView } from "./sourcing";

/**
 * Follows `client-logo.test.tsx`'s pattern: modules the component depends on
 * are mocked directly rather than faking `fetch` for every one of them, since
 * this screen pulls from `sourcing.ts`, `eligibility.ts`, `candidates.ts` (via
 * `namesFor`) and now `clients.ts` all at once.
 *
 * allow-hardcode: the strings below are test fixtures.
 */

const getClient = vi.fn();

vi.mock("./clients", async () => {
  const actual = await vi.importActual<typeof import("./clients")>("./clients");
  return { ...actual, getClient: (...args: unknown[]) => getClient(...args) };
});

const getSourcing = vi.fn();

vi.mock("./sourcing", async () => {
  const actual = await vi.importActual<typeof import("./sourcing")>("./sourcing");
  return {
    ...actual,
    getSourcing: (...args: unknown[]) => getSourcing(...args),
    namesFor: async () => new Map(),
    recordSubmission: vi.fn(),
  };
});

vi.mock("./eligibility", async () => {
  const actual = await vi.importActual<typeof import("./eligibility")>("./eligibility");
  return { ...actual, eligibilityFor: async () => new Map() };
});

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-07-30T00:00:00Z",
    company_name_raw: "Meridian Partners",
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
    ...overrides,
  } as Opportunity;
}

function run(overrides: Partial<SourcingRun> = {}): SourcingRun {
  return {
    id: "run-1",
    opportunity_id: "op-1",
    state: "done",
    client_id: null,
    client_unresolved_reason: null,
    candidates_considered: 3,
    shortlisted: 0,
    protected_attribute_noticed: false,
    protected_attribute_note: null,
    failure_reason: null,
    created_at: "2026-07-30T00:00:00Z",
    ...overrides,
  };
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
    mentions: [],
    contacts: [],
    ...overrides,
  } as Client;
}

function view(data: SourcingView): SourcingView {
  return data;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  getClient.mockReset();
  getSourcing.mockReset();
});

describe("Shortlist client identification", () => {
  it("fetches and renders the client's name and logo when client_id is resolved", async () => {
    getSourcing.mockResolvedValue(view({ run: run({ client_id: "cl-1" }), matches: [] }));
    getClient.mockResolvedValue(client());
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => null }),
    );

    render(<Shortlist row={opportunity()} />);

    await screen.findByText("Meridian Partners");
    await waitFor(() => expect(getClient).toHaveBeenCalledWith("cl-1"));
  });

  it("renders neither name nor logo, and fetches no client, when client_id is null", async () => {
    getSourcing.mockResolvedValue(
      view({
        run: run({ client_id: null, client_unresolved_reason: "No client mention in this email." }),
        matches: [],
      }),
    );

    render(<Shortlist row={opportunity()} />);

    await screen.findByText("No client mention in this email.");
    expect(getClient).not.toHaveBeenCalled();
    expect(screen.queryByText("Meridian Partners")).toBeNull();
  });
});
