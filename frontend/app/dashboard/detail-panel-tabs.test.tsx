import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DetailPanel } from "./detail-panel";
import type {
  Intelligence,
  IntelligenceView,
} from "./job-intelligence";
import type { Opportunity } from "./opportunities";

/**
 * Which tab the job order modal opens on.
 *
 * A job order whose analysis already exists opens on Work — that is the
 * content a returning reader came for. Two rules ride along:
 *
 * 1. No analysis (or one still pending/failed) stays on Origin, where the
 *    Run button and the provenance live.
 * 2. Once the recruiter clicks any tab themselves, the auto-switch never
 *    fires again — a deliberate navigation must not be overruled by the
 *    next poll refetch handing back a new `analysis` object.
 *
 * `useJobIntelligence` is mocked at the module boundary and controlled per
 * test through `hookState`, so each scenario decides what the hook reports
 * without any network.
 *
 * allow-hardcode: the strings below are verbatim copies of user-facing
 * labels being asserted on.
 */

vi.mock("./job-orders-sourcing", () => ({ Shortlist: () => null }));
vi.mock("./job-intelligence-panel", () => ({
  WorkStage: () => <div data-testid="work-stage" />,
  PersonStage: () => <div data-testid="person-stage" />,
  SearchStage: () => <div data-testid="search-stage" />,
}));
vi.mock("../auth", () => ({
  useAuth: () => ({
    status: "signed-in",
    me: {
      user: {
        id: "u-1",
        email: "u@agency.sg",
        display_name: "Someone",
        preferred_name: null,
        role: "recruiter",
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
    } as never,
  }),
}));

let hookState: {
  view: IntelligenceView | null;
  analysis: Intelligence | null;
};

vi.mock("./job-intelligence", () => ({
  useJobIntelligence: () => ({
    phase: { status: "idle", view: hookState.view ?? { intelligence: null } },
    run: async () => {},
    starting: false,
    waiting: false,
    runError: null,
    view: hookState.view,
    analysis: hookState.analysis,
  }),
}));

function opportunity(): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-07-30T00:00:00Z",
    company_name_raw: "Sunrise Care Pte Ltd",
    job_title_raw: "Carer",
    salary_raw: "$2,000/month",
    salary_min: 2000,
    salary_max: 2000,
    salary_currency: "SGD",
    salary_period: "month",
    working_hours_raw: "Mon–Fri",
    requirements: "At least 1 year.",
    job_description: "Care for an elderly client.",
    duration_raw: "6-month contract",
    location_raw: "West",
    employment_type: null,
    quality_state: "verified",
    review_status: "new",
    internet_message_id: null,
    graph_message_id: null,
    verified_fields: 0,
    total_fields: 0,
    placement_type: null,
    sex_requirement: null,
    sex_requirement_reason: null,
    assigned_user_id: "u-1",
    assignee_name: "Wei Kian",
    client_id: null,
    client_name: null,
    source: "pipeline",
    shared_with_me: false,
  } as Opportunity;
}

function doneView(): IntelligenceView {
  return {
    id: "ji-1",
    state: "done",
    failure_reason: null,
    analysed_at: "2026-08-27T01:00:00Z",
    intelligence: {} as Intelligence,
  };
}

function panel() {
  return (
    <DetailPanel
      row={opportunity()}
      onClose={() => {}}
      onReview={async () => null}
      onClaim={async () => ({ ok: true }) as never}
      onAssign={async () => ({ ok: true }) as never}
      onClientSet={async () => ({ ok: true }) as never}
      onEdited={() => {}}
      onVanished={() => {}}
    />
  );
}

afterEach(() => {
  cleanup();
});

describe("which tab the job order modal opens on", () => {
  it("lands on Work when an analysis result already exists", async () => {
    hookState = { view: doneView(), analysis: {} as Intelligence };
    render(panel());
    await waitFor(() =>
      expect(screen.queryByTestId("work-stage")).toBeTruthy(),
    );
    // The Origin facts are not rendered while Work shows.
    expect(screen.getByRole("tab", { selected: true }).textContent).toBe("Work");
  });

  it("stays on Origin when no analysis exists", async () => {
    hookState = { view: null, analysis: null };
    render(panel());
    const selected = screen.getByRole("tab", { selected: true });
    expect(selected.textContent).toBe("Origin");
    expect(screen.queryByTestId("work-stage")).toBeNull();
  });

  it("stays on Origin while the analysis is only pending", async () => {
    hookState = {
      view: {
        id: "ji-1",
        state: "pending",
        failure_reason: null,
        analysed_at: null,
        intelligence: null,
      },
      analysis: null,
    };
    render(panel());
    expect(screen.getByRole("tab", { selected: true }).textContent).toBe(
      "Origin"
    );
  });

  it("does not overrule a tab the recruiter already picked", async () => {
    // First render with nothing — the recruiter clicks Person while the
    // analysis is still running; then the refetch lands with the result.
    hookState = { view: null, analysis: null };
    const { rerender } = render(panel());
    fireEvent.click(screen.getByRole("tab", { name: "Person" }));
    expect(screen.getByTestId("person-stage")).toBeTruthy();

    hookState = { view: doneView(), analysis: {} as Intelligence };
    rerender(panel());
    await waitFor(() =>
      expect(screen.queryByTestId("work-stage")).toBeNull(),
    );
    expect(screen.getByRole("tab", { selected: true }).textContent).toBe(
      "Person"
    );
  });
});
