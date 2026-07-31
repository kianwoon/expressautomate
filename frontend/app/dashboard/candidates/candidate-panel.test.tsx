import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../../auth";
import type { Candidate } from "../candidates";
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
 * - the Edit button's disabled state is exactly `!row.can_edit` — never a
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
      onEdit={() => {}}
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

  it("disables Edit exactly when can_edit is false, and says why", async () => {
    render(panel(candidate({ owner: { id: "u-2", name: "Sarah Lim" }, can_edit: false })));
    const edit = screen.getByRole("button", { name: "Edit" });
    expect(edit.hasAttribute("disabled")).toBe(true);
    expect(
      screen.getByText(/Sarah Lim holds this candidate/),
    ).toBeTruthy();
  });

  it("enables Edit when can_edit is true, even for a colleague's row", async () => {
    // The owner role can edit anyone's candidate — can_edit says so; nothing
    // here re-derives it from owner.id === me.id.
    render(panel(candidate({ owner: { id: "u-2", name: "Sarah Lim" }, can_edit: true })));
    const edit = screen.getByRole("button", { name: "Edit" });
    expect(edit.hasAttribute("disabled")).toBe(false);
  });
});
