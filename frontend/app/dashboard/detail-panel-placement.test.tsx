import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DetailPanel } from "./detail-panel";
import type { Opportunity } from "./opportunities";

/**
 * A colleague's placement change, arriving while the panel is open.
 *
 * The list polls, so a change made at another desk reaches this panel as a
 * fresh `row`. The panel used to seed its placement copy once and update it
 * only from its own saves, so the recruiter kept reading the old values — and
 * `placement_type` gates a lawful sex filter and both fields stamp an audited
 * `set_by`/`set_at`, so saving over a judgement nobody was shown is worse than
 * the usual stale-state bug.
 *
 * `./job-orders-sourcing` is stubbed because the shortlist reads its own
 * endpoint and has its own file.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

vi.mock("./job-orders-sourcing", () => ({ Shortlist: () => null }));

vi.mock("../auth", () => ({
  useAuth: () => ({ user: { id: "u-1", role: "recruiter" } }),
}));

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-07-30T00:00:00Z",
    company_name_raw: "Sunrise Care Pte Ltd",
    job_title_raw: "Carer",
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
    assigned_user_id: "u-1",
    assignee_name: "Wei Kian",
    client_id: null,
    client_name: null,
    source: "pipeline",
    shared_with_me: false,
    ...overrides,
  } as Opportunity;
}

function panel(row: Opportunity) {
  return (
    <DetailPanel
      row={row}
      onClose={() => {}}
      onReview={async () => null}
      onClaim={async () => ({ ok: true }) as never}
      onAssign={async () => ({ ok: true }) as never}
      onClientSet={async () => ({ ok: true }) as never}
      onVanished={() => {}}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the panel and a colleague's placement change", () => {
  it("shows the placement type a poll brought in", () => {
    const { rerender } = render(panel(opportunity()));
    expect(screen.getByDisplayValue("Not set")).toBeDefined();

    // The same row, as the next poll returns it after a colleague set it.
    rerender(panel(opportunity({ placement_type: "mdw_work_permit" })));

    const select = screen.getByDisplayValue("MDW Work Permit") as HTMLSelectElement;
    expect(select.value).toBe("mdw_work_permit");
  });

  it("leaves an unsaved choice alone when someone else's change lands", () => {
    const { rerender } = render(panel(opportunity()));
    const select = screen.getByDisplayValue("Not set") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "local_hire" } });

    rerender(panel(opportunity({ placement_type: "mdw_work_permit" })));

    // The recruiter's own half-made choice survives: replacing what someone is
    // in the middle of choosing is its own way of losing a decision. Save
    // still sends it, and the server holds the last write either way.
    expect(select.value).toBe("local_hire");
  });
});
