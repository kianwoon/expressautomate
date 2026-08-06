import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobIntelligence } from "./job-intelligence-panel";
import type { Intelligence, IntelligenceView } from "./job-intelligence";
import type { Opportunity } from "./opportunities";

// Poll quickly in tests so the polling-flow test does not wait the real 4s.
vi.mock("../api", async () => ({
  ...(await vi.importActual<typeof import("../api")>("../api")),
  SOURCING_POLL_MS: 10,
}));

/**
 * Mocks the data module the panel depends on, following the pattern in
 * `job-orders-sourcing.test.tsx`: the component's fetches are stubbed directly
 * rather than faking `fetch`. The panel now polls GET until a row is terminal,
 * so several tests queue successive GET responses.
 *
 * allow-hardcode: the strings below are test fixtures.
 */

const getIntelligence = vi.fn();
const runIntelligence = vi.fn();

vi.mock("./job-intelligence", async () => {
  // Use the REAL `inFlight`, not a stub: a stub that disagrees with the real
  // one hid a bug where a `pending` row (intelligence: null) stopped the
  // poller the instant a run started. The fetches are still stubbed.
  const actual = await vi.importActual<typeof import("./job-intelligence")>("./job-intelligence");
  return {
    ...actual,
    getIntelligence: () => getIntelligence(),
    runIntelligence: (id: string) => runIntelligence(id),
  };
});

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-08-06T00:00:00Z",
    company_name_raw: "Acme",
    job_title_raw: "Logistics Manager",
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

function analysis(overrides: Partial<Intelligence> = {}): Intelligence {
  return {
    understanding: {
      role: "Logistics Manager",
      business_purpose: "Keep the warehouse running",
      daily_activities: ["Plan dispatch"],
      work_environment: "Warehouse",
      must_have_requirements: ["3 years logistics"],
      preferred_requirements: [],
      working_conditions: "Onsite",
      success_characteristics: [],
      potential_challenges: [],
      confidence: 0.8,
    },
    persona: {
      likely_backgrounds: ["Logistics coordinator"],
      transferable_roles: [],
      transferable_industries: [],
      behaviours: [],
      communication_style: "Direct",
      career_stage: "Mid-level",
      motivations: [],
      salary_expectation: "SGD 4,500/month",
      availability: "1 month",
    },
    search_plan: {
      platform: "LinkedIn",
      priority: 2,
      queries: ['"logistics manager" AND dispatch'],
      negative_queries: ["intern"],
      salary: "SGD 4,500/month",
      location: "Singapore",
      employment_type: "Full-time",
    },
    ...overrides,
  };
}

function doneView(overrides: Partial<IntelligenceView> = {}): IntelligenceView {
  return {
    id: "row-1",
    state: "done",
    failure_reason: null,
    analysed_at: "2026-08-06T09:00:00Z",
    intelligence: analysis(),
    removed_codes: [],
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("JobIntelligence panel", () => {
  it("shows the 'no analysis yet' prompt when GET returns null", async () => {
    getIntelligence.mockResolvedValue({ intelligence: null });
    render(<JobIntelligence row={opportunity()} />);
    await waitFor(() => {
      expect(screen.getByText(/No analysis yet/i)).toBeDefined();
    });
    expect(getIntelligence).toHaveBeenCalled();
  });

  it("renders the three stages when the row is done", async () => {
    getIntelligence.mockResolvedValue(doneView());
    render(<JobIntelligence row={opportunity()} />);

    await waitFor(() => {
      expect(screen.getByText("Logistics Manager")).toBeDefined();
    });
    expect(screen.getByText("Understanding the work")).toBeDefined();
    expect(screen.getByText("The ideal person")).toBeDefined();
    expect(screen.getByText("How to find them")).toBeDefined();
    expect(screen.getByText('"logistics manager" AND dispatch')).toBeDefined();
  });

  it("renders the result by polling, without reopening the modal", async () => {
    // Regression: a `pending` row has intelligence: null, and the poller once
    // treated that as "not in flight", so the result never arrived until the
    // modal was closed and reopened (which remounted the panel). Now the poller
    // keys off `state`, so a run flows pending → done in place.
    getIntelligence.mockResolvedValue({ intelligence: null }); // mount: nothing yet
    runIntelligence.mockResolvedValue({
      id: "row-1",
      state: "pending",
      failure_reason: null,
      analysed_at: null,
      intelligence: null,
    });
    render(<JobIntelligence row={opportunity()} />);

    await waitFor(() => {
      screen.getByRole("button", { name: /Run analysis/i }).click();
    });
    await waitFor(() => {
      expect(runIntelligence).toHaveBeenCalledWith("op-1");
    });

    // The first poll returns pending (still working)...
    getIntelligence.mockResolvedValueOnce({
      id: "row-1",
      state: "pending",
      failure_reason: null,
      analysed_at: null,
      intelligence: null,
    });
    // ...the next poll returns done with the analysis.
    getIntelligence.mockResolvedValueOnce(doneView());

    // The result appears without any remount — no close/reopen.
    await waitFor(() => {
      expect(screen.getByText("Understanding the work")).toBeDefined();
    });
    expect(screen.getByText("Logistics Manager")).toBeDefined();
  });

  it("shows the failure reason when the row is failed", async () => {
    getIntelligence.mockResolvedValue({
      id: "row-1",
      state: "failed",
      failure_reason: "This job order has no title to analyse.",
      analysed_at: null,
      intelligence: null,
    });
    render(<JobIntelligence row={opportunity()} />);
    await waitFor(() => {
      expect(screen.getByText(/no title to analyse/i)).toBeDefined();
    });
  });

  it("names the protected-attribute codes that were withheld", async () => {
    getIntelligence.mockResolvedValue(doneView({ removed_codes: ["C/F", "O/F"] }));
    render(<JobIntelligence row={opportunity()} />);
    await waitFor(() => {
      expect(screen.getByText(/withheld from the analysis/i)).toBeDefined();
    });
    expect(screen.getByText(/C\/F, O\/F/)).toBeDefined();
  });
});
