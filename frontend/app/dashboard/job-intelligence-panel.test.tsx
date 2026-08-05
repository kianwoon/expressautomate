import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobIntelligence } from "./job-intelligence-panel";
import type { Intelligence, IntelligenceView } from "./job-intelligence";
import type { Opportunity } from "./opportunities";

/**
 * Mocks the data module the panel depends on, following the pattern in
 * `job-orders-sourcing.test.tsx`: the component's fetches are stubbed directly
 * rather than faking `fetch`, so the test asserts on what the panel renders for
 * each view the API can return.
 *
 * allow-hardcode: the strings below are test fixtures.
 */

const getIntelligence = vi.fn(async (): Promise<IntelligenceView> => ({ intelligence: null }));
const runIntelligence = vi.fn(async (): Promise<IntelligenceView> => ({ intelligence: null }));

vi.mock("./job-intelligence", () => ({
  getIntelligence: () => getIntelligence(),
  runIntelligence: () => runIntelligence(),
}));

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

  it("renders the three stages when an analysis exists", async () => {
    getIntelligence.mockResolvedValue({
      intelligence: analysis(),
      removed_codes: [],
      analysed_at: "2026-08-06T09:00:00Z",
    });
    render(<JobIntelligence row={opportunity()} />);

    await waitFor(() => {
      expect(screen.getByText("Logistics Manager")).toBeDefined();
    });
    expect(screen.getByText("Understanding the work")).toBeDefined();
    expect(screen.getByText("The ideal person")).toBeDefined();
    expect(screen.getByText("How to find them")).toBeDefined();
    // A query string rendered.
    expect(screen.getByText('"logistics manager" AND dispatch')).toBeDefined();
  });

  it("runs the analysis when the button is clicked", async () => {
    getIntelligence.mockResolvedValue({ intelligence: null });
    runIntelligence.mockResolvedValue({
      intelligence: analysis(),
      removed_codes: [],
      analysed_at: "2026-08-06T09:00:00Z",
    });
    render(<JobIntelligence row={opportunity()} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Run analysis/i })).toBeDefined();
    });

    screen.getByRole("button", { name: /Run analysis/i }).click();

    await waitFor(() => {
      expect(runIntelligence).toHaveBeenCalled();
    });
    // The result replaces the empty prompt.
    await waitFor(() => {
      expect(screen.getByText("Understanding the work")).toBeDefined();
    });
  });

  it("reports a server error from the run, in the server's words", async () => {
    getIntelligence.mockResolvedValue({ intelligence: null });
    const { ApiError } = await import("./candidates");
    runIntelligence.mockRejectedValue(new ApiError("This job order has no title to analyse."));
    render(<JobIntelligence row={opportunity()} />);

    await waitFor(() => {
      screen.getByRole("button", { name: /Run analysis/i }).click();
    });
    await waitFor(() => {
      expect(screen.getByText(/no title to analyse/i)).toBeDefined();
    });
  });

  it("names the protected-attribute codes that were withheld", async () => {
    getIntelligence.mockResolvedValue({
      intelligence: analysis(),
      removed_codes: ["C/F", "O/F"],
      analysed_at: "2026-08-06T09:00:00Z",
    });
    render(<JobIntelligence row={opportunity()} />);
    await waitFor(() => {
      expect(screen.getByText(/withheld from the analysis/i)).toBeDefined();
    });
    expect(screen.getByText(/C\/F, O\/F/)).toBeDefined();
  });
});
