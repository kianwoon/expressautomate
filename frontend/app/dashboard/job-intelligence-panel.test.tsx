import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PersonStage, SearchStage, WorkStage, type StageState } from "./job-intelligence-panel";
import { type Intelligence, type IntelligenceView, useJobIntelligence } from "./job-intelligence";

/**
 * The panel split into a hook (`useJobIntelligence`) and three presentational
 * stage components. The hook is tested through a small harness that renders it;
 * the stages are tested directly from canned intelligence slices.
 *
 * The hook calls `runIntelligence`/`getIntelligence`, which are bare local
 * references in the same module — so mocking the export does not intercept
 * them. The honest seam is `fetch`, which is what those functions ultimately
 * call, so the hook test stubs `globalThis.fetch`.
 *
 * allow-hardcode: the strings below are test fixtures.
 */

// Poll quickly in tests so the polling-flow test does not wait the real 4s.
vi.mock("../api", async () => ({
  ...(await vi.importActual<typeof import("../api")>("../api")),
  SOURCING_POLL_MS: 10,
}));

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

/** A harness that calls the hook and stashes the result for assertions. */
function HookHarness({ rowId }: { rowId: string }) {
  const ji = useJobIntelligence(rowId);
  return (
    <div>
      <button onClick={() => void ji.run()}>run</button>
      <span data-testid="starting">{String(ji.starting)}</span>
      <span data-testid="waiting">{String(ji.waiting)}</span>
      <span data-testid="has-analysis">{String(!!ji.analysis)}</span>
    </div>
  );
}

/** A JSON response for the fetch stub. */
function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    json: async () => body,
  } as Response;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("useJobIntelligence hook", () => {
  it("reports no analysis when GET returns null", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(jsonResponse({ intelligence: null }));
    render(<HookHarness rowId="op-1" />);
    await waitFor(() => {
      expect(screen.getByTestId("has-analysis").textContent).toBe("false");
    });
    // The mount GET hit the intelligence endpoint.
    expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith("/intelligence"))).toBe(true);
  });

  it("runs and polls pending → done, reporting analysis when it lands", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");

    // mount GET: nothing yet
    fetchMock.mockResolvedValueOnce(jsonResponse({ intelligence: null }));
    render(<HookHarness rowId="op-1" />);

    await waitFor(() => {
      expect(screen.getByTestId("has-analysis").textContent).toBe("false");
    });

    // POST (run): returns pending.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: "row-1",
        state: "pending",
        failure_reason: null,
        analysed_at: null,
        intelligence: null,
      }),
    );
    // First poll GET: still pending.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: "row-1",
        state: "pending",
        failure_reason: null,
        analysed_at: null,
        intelligence: null,
      }),
    );
    // Second poll GET: done.
    fetchMock.mockResolvedValueOnce(jsonResponse(doneView()));

    // Start the run.
    screen.getByText("run").click();

    await waitFor(() => {
      expect(screen.getByTestId("has-analysis").textContent).toBe("true");
    });
    // The POST was made (runIntelligence).
    const postCall = fetchMock.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === "POST",
    );
    expect(postCall).toBeDefined();
  });
});

describe("stage panels", () => {
  const empty: StageState = {
    hasAnalysis: false,
    waiting: false,
    failed: false,
    failureReason: null,
    loading: false,
    readError: null,
  };

  it("WorkStage shows the understanding fields when analysis exists", () => {
    const a = analysis();
    render(<WorkStage intelligence={a} state={{ ...empty, hasAnalysis: true }} />);
    expect(screen.getByText("Understanding the work")).toBeDefined();
    expect(screen.getByText("Logistics Manager")).toBeDefined();
    expect(screen.getByText("Plan dispatch")).toBeDefined();
  });

  it("WorkStage shows the nothing-yet notice when no analysis", () => {
    render(<WorkStage intelligence={null} state={empty} />);
    expect(screen.getByText(/No analysis yet/i)).toBeDefined();
  });

  it("PersonStage shows the persona fields when analysis exists", () => {
    const a = analysis();
    render(<PersonStage intelligence={a} state={{ ...empty, hasAnalysis: true }} />);
    expect(screen.getByText("The ideal person")).toBeDefined();
    expect(screen.getByText("Logistics coordinator")).toBeDefined();
    expect(screen.getByText("Mid-level")).toBeDefined();
  });

  it("SearchStage shows the search fields and query strings", () => {
    const a = analysis();
    const view = doneView();
    render(
      <SearchStage intelligence={a} state={{ ...empty, hasAnalysis: true }} view={view} />,
    );
    expect(screen.getByText("How to find them")).toBeDefined();
    expect(screen.getByText("LinkedIn")).toBeDefined();
    expect(screen.getByText('"logistics manager" AND dispatch')).toBeDefined();
  });

  it("SearchStage names the protected-attribute codes that were withheld", () => {
    const a = analysis();
    const view = doneView({ removed_codes: ["C/F", "O/F"] });
    render(
      <SearchStage intelligence={a} state={{ ...empty, hasAnalysis: true }} view={view} />,
    );
    expect(screen.getByText(/withheld from the analysis/i)).toBeDefined();
    expect(screen.getByText(/C\/F, O\/F/)).toBeDefined();
  });

  it("a stage shows the failure reason when the run failed", () => {
    const failed: StageState = {
      ...empty,
      failed: true,
      failureReason: "This job order has no title to analyse.",
    };
    render(<WorkStage intelligence={null} state={failed} />);
    expect(screen.getByText(/no title to analyse/i)).toBeDefined();
  });
});
