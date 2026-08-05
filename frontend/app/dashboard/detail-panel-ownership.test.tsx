import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../auth";
import { DetailPanel } from "./detail-panel";
import { resetMembers } from "./members";
import type { MutationResult, Opportunity } from "./opportunities";

/**
 * Ownership, where a recruiter actually decides: the detail panel.
 *
 * Claim is here and nowhere else. Taking a job order is taking responsibility
 * for it, and that should follow reading the thing rather than scanning a row
 * at 9pm — so there is no claim button on the list, and this file is where the
 * whole affordance is pinned.
 *
 * The two failure shapes are the interesting ones. A 404 means the row went
 * away under an open panel — a share withdrawn, or an owner reassigning — and
 * that is a closed state, not an error: the panel clears and says so once. A
 * 403 is the only message whose right wording depends on the row rather than
 * the status, because "shared with you, not assigned to you" is a lie about a
 * job order nobody was ever assigned.
 *
 * `Shortlist` is stubbed: it reads its own endpoint and has its own test file,
 * and nothing here is about the wire.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

vi.mock("./job-orders-sourcing", () => ({ Shortlist: () => null }));
vi.mock("./job-intelligence-panel", () => ({ JobIntelligence: () => null }));

let authState: Me | null = null;

vi.mock("../auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../auth")>();
  return {
    ...actual,
    useAuth: () =>
      authState ? { status: "signed-in", me: authState } : { status: "loading" },
  };
});

const STAFF = [
  { id: "u-1", name: "Priya Nair", email: "priya@agency.sg", role: "member" },
  { id: "u-2", name: "Wei Ling", email: "wei@agency.sg", role: "member" },
  { id: "u-3", name: "Boss Tan", email: "boss@agency.sg", role: "owner" },
];

function me(id: string, role: string): Me {
  return {
    user: {
      id,
      email: `${id}@agency.sg`,
      display_name: "Someone",
      preferred_name: null,
      role,
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

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-07-30T00:00:00Z",
    company_name_raw: "Acme",
    job_title_raw: "Care assistant",
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

const ok: MutationResult = { ok: true };

function panel(
  row: Opportunity,
  handlers: {
    onClaim?: (id: string) => Promise<MutationResult>;
    onAssign?: (id: string, userId: string | null) => Promise<MutationResult>;
    onVanished?: (id: string) => void;
  } = {},
) {
  return (
    <DetailPanel
      row={row}
      onClose={() => {}}
      onReview={async () => null}
      onClaim={handlers.onClaim ?? (async () => ok)}
      onAssign={handlers.onAssign ?? (async () => ok)}
      onClientSet={async () => ok}
      onVanished={handlers.onVanished ?? (() => {})}
    />
  );
}

/** The owner line, as one string. */
function ownerLine(): string {
  return screen.getByTestId("jo-detail-owner").textContent ?? "";
}

beforeEach(() => {
  authState = me("u-1", "member");
  resetMembers();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => STAFF,
    }),
  );
});

afterEach(() => {
  cleanup();
  resetMembers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("who holds a job order", () => {
  it("names the owner", async () => {
    render(panel(opportunity({ assigned_user_id: "u-2", assignee_name: "Wei Ling" })));
    expect(ownerLine()).toContain("Wei Ling");
  });

  it("says Unassigned for a queue item", async () => {
    render(panel(opportunity()));
    expect(ownerLine()).toContain("Unassigned");
  });

  it("offers Claim only when unassigned", async () => {
    const { unmount } = render(panel(opportunity()));
    expect(screen.getByRole("button", { name: "Claim this job order" })).toBeTruthy();
    unmount();

    render(panel(opportunity({ assigned_user_id: "u-2", assignee_name: "Wei Ling" })));
    expect(screen.queryByRole("button", { name: "Claim this job order" })).toBeNull();
  });

  it("shows the 409 sentence when someone else claims first", async () => {
    const onClaim = vi.fn().mockResolvedValue({
      ok: false,
      kind: "conflict",
      message: "Someone else has taken this one.",
    });
    render(panel(opportunity(), { onClaim }));

    fireEvent.click(screen.getByRole("button", { name: "Claim this job order" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Someone else has taken this one.",
    );
  });

  it("reports upward on a 404 so the modal can close, without an error line", async () => {
    // A share was withdrawn under the open modal. A 404 is not an error to
    // display beside the fields — the fields are stale the moment it arrives —
    // so the detail reports the id upward and leaves closing to the parent
    // (which owns the `selected` state and unmounts the modal). Rendered
    // standalone here, so the mock `onVanished` is what we assert on: the
    // detail's only job is to call it, and to stay quiet (no red alert).
    const onVanished = vi.fn();
    const onClaim = vi.fn().mockResolvedValue({
      ok: false,
      kind: "gone",
      message: "This job order is no longer available.",
    });
    render(panel(opportunity(), { onClaim, onVanished }));

    fireEvent.click(screen.getByRole("button", { name: "Claim this job order" }));

    await waitFor(() => expect(onVanished).toHaveBeenCalledWith("op-1"));
    // A close, not an error: nothing red is shown for it.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says to claim it first when a 403 comes back on an unassigned job order", async () => {
    // "Shared with you, not assigned to you" would be a lie here — nobody was
    // assigned it.
    const onClaim = vi.fn().mockResolvedValue({
      ok: false,
      kind: "forbidden",
      message: "This job order is not yours to reassign.",
    });
    render(panel(opportunity(), { onClaim }));

    fireEvent.click(screen.getByRole("button", { name: "Claim this job order" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Claim this job order before editing it.",
    );
  });

  it("says shared-not-assigned on a 403 for an assigned one", async () => {
    authState = me("u-3", "owner");
    const onAssign = vi.fn().mockResolvedValue({
      ok: false,
      kind: "forbidden",
      message: "This job order is not yours to reassign.",
    });
    render(
      panel(
        opportunity({ assigned_user_id: "u-2", assignee_name: "Wei Ling", shared_with_me: true }),
        { onAssign },
      ),
    );

    const select = await screen.findByLabelText("Assign to");
    await waitFor(() => expect((select as HTMLSelectElement).disabled).toBe(false));
    fireEvent.change(select, { target: { value: "u-1" } });

    expect((await screen.findByRole("alert")).textContent).toContain(
      "This job order is shared with you, not assigned to you.",
    );
  });

  it("offers Assign to the owner role and to the assignee, and to nobody else", async () => {
    const assigned = opportunity({ assigned_user_id: "u-2", assignee_name: "Wei Ling" });

    // The agency owner may hand any job order on.
    authState = me("u-3", "owner");
    const asOwner = render(panel(assigned));
    expect(await screen.findByLabelText("Assign to")).toBeTruthy();
    asOwner.unmount();

    // So may whoever currently holds it.
    authState = me("u-2", "member");
    const asAssignee = render(panel(assigned));
    expect(await screen.findByLabelText("Assign to")).toBeTruthy();
    asAssignee.unmount();

    // A colleague it is merely shared with may not.
    authState = me("u-1", "member");
    const asBystander = render(panel(assigned));
    await waitFor(() =>
      expect(within(asBystander.container).queryByText("Wei Ling", { exact: false })).toBeTruthy(),
    );
    expect(screen.queryByLabelText("Assign to")).toBeNull();
  });
});
