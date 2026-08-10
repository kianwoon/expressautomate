import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../../auth";
import type { Candidate } from "../candidates";
import { findCandidateJobs } from "../candidate-jobs";
import { resetMembers } from "../members";
import { CandidatePanel } from "./candidate-panel";

/**
 * Ownership on the candidate panel, since 2c6051f put `owner` and `can_edit`
 * on the wire.
 *
 * Two rules this pins:
 * - the Owner row reads the server's `owner.name` directly — it must NOT
 *   resolve the name a second time through `useMembers()`, which is a second
 *   place for the name to disagree with the server's.
 * - the Save button's disabled state is exactly `!row.can_edit` — never a
 *   re-derived `owner.id === me.id` comparison, so the UI cannot drift from
 *   the server's rule.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

vi.mock("./candidate-cv", () => ({ CandidateCv: () => null }));
vi.mock("./candidate-history", () => ({ CandidateHistory: () => null }));
vi.mock("./candidate-whatsapp", () => ({
  WhatsappActivityTimeline: () => null,
  WhatsappButton: () => null,
}));
vi.mock("../candidate-jobs", () => ({ findCandidateJobs: vi.fn() }));

let authState: Me | null = null;

vi.mock("../../auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../auth")>();
  return {
    ...actual,
    useAuth: () => (authState ? { status: "signed-in", me: authState } : { status: "loading" }),
  };
});

function me(id: string, role: string): Me {
  return {
    user: { id, email: `${id}@agency.sg`, display_name: "Someone", preferred_name: null, role },
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

function candidate(overrides: Partial<Candidate> = {}): Candidate {
  return {
    id: "cand-1",
    full_name: "Wei Ming Tan",
    email: null,
    phone_raw: null,
    phone_e164: null,
    current_title: null,
    current_employer: null,
    location: null,
    years_experience: null,
    expected_salary: null,
    salary_currency: null,
    salary_period: null,
    available_from: null,
    notice_period_raw: null,
    employment_type: null,
    notes: null,
    pipeline_stage: "new",
    record_status: "active",
    updated_at: "2026-07-30T00:00:00Z",
    owner: null,
    can_edit: true,
    merged_into_candidate_id: null,
    avatar_key: null,
    avatar_updated_at: null,
    ...overrides,
  } as Candidate;
}

beforeEach(() => {
  authState = me("u-1", "member");
  resetMembers();
  vi.mocked(findCandidateJobs).mockResolvedValue({
    items: [],
    considered: 0,
    scored: 0,
    limit: 5,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [] }),
  );
});

afterEach(() => {
  cleanup();
  resetMembers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function panel(row: Candidate) {
  return (
    <CandidatePanel
      row={row}
      onClose={() => {}}
      onArchive={async () => {}}
      onRestore={async () => {}}
      onDelete={null}
      onChanged={() => {}}
      onDetailChanged={() => {}}
    />
  );
}

describe("who holds a candidate, on the panel", () => {
  it("says who owns it, from the server's owner.name — not a members lookup", async () => {
    render(panel(candidate({ owner: { id: "u-2", name: "Sarah Lim" }, can_edit: false })));
    expect(await screen.findByText("Sarah Lim")).toBeTruthy();
  });

  it("says unclaimed when owner is null", async () => {
    render(panel(candidate({ owner: null, can_edit: true })));
    expect(
      await screen.findByText(/Unclaimed — anyone at the agency can take this one/),
    ).toBeTruthy();
  });

  it("disables Save exactly when can_edit is false, and says why", async () => {
    render(panel(candidate({ owner: { id: "u-2", name: "Sarah Lim" }, can_edit: false })));
    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save.hasAttribute("disabled")).toBe(true);
    expect(
      screen.getByText(/Sarah Lim holds this candidate/),
    ).toBeTruthy();
  });

  it("enables Save when can_edit is true, even for a colleague's row", async () => {
    // The owner role can edit anyone's candidate — can_edit says so; nothing
    // here re-derives it from owner.id === me.id.
    render(panel(candidate({ owner: { id: "u-2", name: "Sarah Lim" }, can_edit: true })));
    const save = screen.getByRole("button", { name: "Save changes" });
    expect(save.hasAttribute("disabled")).toBe(false);
  });
});

describe("Find Job on the candidate panel", () => {
  it("shows the shortlist after Find Job, best first, with the score", async () => {
    vi.mocked(findCandidateJobs).mockResolvedValue({
      items: [
        {
          id: "jo-1",
          company_name_raw: "Acme Health",
          job_title_raw: "Staff Nurse",
          location_raw: "Singapore",
          salary_raw: "SGD 3,000/month",
          salary_min: 2800,
          salary_max: 3500,
          salary_currency: "SGD",
          salary_period: "month",
          working_hours_raw: null,
          duration_raw: null,
          requirements: null,
          employment_type: null,
          assigned_user_id: null,
          received_datetime: "2026-08-01T09:00:00Z",
          score: "0.9508",
          review_status: "new",
          quality_state: "likely",
          reasons: [
            {
              name: "title",
              weight: "3.0",
              raw: "1.0000",
              contribution: "3.0000",
              note: null,
            },
            {
              name: "salary",
              weight: "2.0",
              raw: null,
              contribution: null,
              note: "No comparable salary: one side is missing, or the currencies differ.",
            },
          ],
        },
      ],
      considered: 6,
      scored: 6,
      limit: 5,
    });

    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    expect(await screen.findByText("Staff Nurse")).toBeTruthy();
    expect(screen.getByText(/at Acme Health/)).toBeTruthy();
    // The score renders as a whole percentage, and the breakdown as the share
    // of the component's weight the job order earned.
    expect(screen.getByText("95%")).toBeTruthy();
    expect(screen.getByText("100%")).toBeTruthy();
    // An absent component says what was missing rather than drawing an empty bar.
    expect(
      screen.getByText("No comparable salary: one side is missing, or the currencies differ."),
    ).toBeTruthy();
    expect(findCandidateJobs).toHaveBeenCalledWith("cand-1");
  });

  it("says so when nothing scored high enough", async () => {
    vi.mocked(findCandidateJobs).mockResolvedValue({
      items: [],
      considered: 2,
      scored: 0,
      limit: 5,
    });

    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    expect(
      await screen.findByText(/Nothing scored high enough to be worth showing/),
    ).toBeTruthy();
    expect(screen.getByText(/2 visible job orders were examined/)).toBeTruthy();
  });

  it("shows the server's message when the read fails", async () => {
    vi.mocked(findCandidateJobs).mockRejectedValue(new Error("We could not reach the server."));

    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    expect(await screen.findByText("We could not reach the server.")).toBeTruthy();
  });

  it("lands on the Jobs tab when it runs", async () => {
    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    // The Jobs tab is now the selected one — the shortlist is what the button
    // is for, so the recruiter is taken to it rather than left on Details.
    expect((await screen.findByRole("tab", { name: "Jobs" })).getAttribute("aria-selected")).toBe(
      "true",
    );
  });
});
