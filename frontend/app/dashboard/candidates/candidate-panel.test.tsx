import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../../auth";
import type { Candidate } from "../candidates";
import type { CandidateJobMatch, CandidateJobs } from "../candidate-jobs";
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

/** A Find Job response. `saved_at: null` is the never-run shape. */
function jobsView(
  items: CandidateJobMatch[],
  overrides: Partial<CandidateJobs> = {},
): CandidateJobs {
  return {
    items,
    considered: items.length,
    scored: items.length,
    limit: 5,
    candidate_salary: null,
    saved_at: "2026-08-11T02:00:00Z",
    ...overrides,
  };
}

// What the real `useCandidateJobs` hook receives through the stubbed fetch:
// the mount read (GET) and the run (POST) each answer with their own view, so
// the panel tests exercise the real hook, not a fake one.
let jobsGet: CandidateJobs;
let jobsRun: CandidateJobs;
/** When set, the run (POST) fails with this detail instead of answering. */
let jobsRunError: string | null = null;
let jobsPostCalls = 0;

/** What `useCandidateIntelligence` GETs on mount. `null` = no analysis yet. */
let intelView: Record<string, unknown> = { intelligence: null };

beforeEach(() => {
  authState = me("u-1", "member");
  resetMembers();
  jobsGet = jobsView([], { saved_at: null });
  jobsRun = jobsView([], { saved_at: null });
  jobsRunError = null;
  jobsPostCalls = 0;
  intelView = { intelligence: null };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/jobs")) {
        const method = init?.method ?? "GET";
        if (method === "POST") {
          jobsPostCalls += 1;
          if (jobsRunError)
            return Promise.resolve({
              ok: false,
              status: 500,
              json: async () => ({ detail: jobsRunError }),
            });
          return Promise.resolve({ ok: true, status: 200, json: async () => jobsRun });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => jobsGet });
      }
      if (url.includes("/intelligence")) {
        return Promise.resolve({ ok: true, status: 200, json: async () => intelView });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => [] });
    }),
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
  // The fixture the run answers with: one strong match and an absent salary,
  // the same reasons shape the earlier fixes pinned. `count` repeats the same
  // vacancy so the Jobs-tab count can be exercised without N bespoke fixtures.
  function runView(overrides: Partial<CandidateJobs> = {}, count = 1): CandidateJobs {
    return jobsView(
      Array.from({ length: count }, (_, i) => ({
        id: `jo-${i + 1}`,
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
            name: "semantic",
            weight: "2.0",
            raw: null,
            contribution: null,
            note: "No CV embedding on file.",
          },
          {
            name: "salary",
            weight: "2.0",
            raw: null,
            contribution: null,
            note: "No comparable salary: one side is missing, or the currencies differ.",
          },
        ],
      })),
      {
        considered: 6,
        scored: 6,
        // The candidate has no salary expectation on record, so the salary
        // absence is a candidate-level fact stated once — not on the card.
        candidate_salary: null,
        ...overrides,
      },
    );
  }

  it("shows the shortlist after Find Job, best first, with the score", async () => {
    jobsRun = runView();

    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    expect(await screen.findByText("Staff Nurse")).toBeTruthy();
    expect(screen.getByText(/at Acme Health/)).toBeTruthy();
    // The score renders as a whole percentage, and the breakdown as the share
    // of the component's weight the job order earned.
    expect(screen.getByText("95%")).toBeTruthy();
    expect(screen.getByText("100%")).toBeTruthy();
    // Absent components are stated once below the list, not on every card —
    // and the CV-match component is never shown (it never scores in this
    // direction, and its generic note would mislead).
    expect(screen.queryByText("No CV embedding on file.")).toBeNull();
    expect(
      screen.getByText(/no salary expectation on file.*on every job order/),
    ).toBeTruthy();
    // Exactly one run fired — the button's POST — and no read happened beyond
    // the modal's mount GET.
    expect(jobsPostCalls).toBe(1);
  });

  it("replaces the shown dataset when Find Job runs again", async () => {
    jobsRun = runView();
    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));
    expect(await screen.findByText("Staff Nurse")).toBeTruthy();

    // The agency's vacancies changed; a re-run must swap the old shortlist
    // for the new one, not keep the stale results on the tab.
    jobsRun = runView({
      items: [
        {
          id: "jo-9",
          company_name_raw: "Freight Co",
          job_title_raw: "Warehouse Assistant",
          location_raw: null,
          salary_raw: null,
          salary_min: null,
          salary_max: null,
          salary_currency: null,
          salary_period: null,
          working_hours_raw: null,
          duration_raw: null,
          requirements: null,
          employment_type: null,
          assigned_user_id: null,
          received_datetime: null,
          score: "0.7421",
          review_status: "new",
          quality_state: "likely",
          reasons: [
            {
              name: "title",
              weight: "3.0",
              raw: "0.5000",
              contribution: "1.5000",
              note: null,
            },
          ],
        },
      ],
      considered: 7,
      scored: 7,
    });
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    expect(await screen.findByText("Warehouse Assistant")).toBeTruthy();
    // The old shortlist is gone — replaced, not appended.
    expect(screen.queryByText("Staff Nurse")).toBeNull();
    expect(screen.queryByText(/at Acme Health/)).toBeNull();
    expect(jobsPostCalls).toBe(2);
  });

  it("reopens to the last saved result without running again", async () => {
    // The tab's own action: opening the modal reads the saved snapshot, so the
    // last Find Job result is there when the recruiter clicks Jobs.
    jobsGet = runView();

    render(panel(candidate()));
    fireEvent.click(screen.getByRole("tab", { name: "Jobs" }));

    expect(await screen.findByText("Staff Nurse")).toBeTruthy();
    expect(screen.getByText(/Last run/)).toBeTruthy();
    // No run happened: only the mount read, never a POST.
    expect(jobsPostCalls).toBe(0);
  });

  it("keeps the generic salary note when the candidate has a salary but a job order lacks one", async () => {
    jobsRun = runView({
      items: [
        {
          id: "jo-1",
          company_name_raw: "Acme Health",
          job_title_raw: "Staff Nurse",
          location_raw: null,
          salary_raw: null,
          salary_min: null,
          salary_max: null,
          salary_currency: null,
          salary_period: null,
          working_hours_raw: null,
          duration_raw: null,
          requirements: null,
          employment_type: null,
          assigned_user_id: null,
          received_datetime: null,
          score: "0.6823",
          review_status: "new",
          quality_state: "likely",
          reasons: [
            {
              name: "title",
              weight: "3.0",
              raw: "0.6667",
              contribution: "2.0001",
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
      considered: 1,
      scored: 1,
      // The candidate's own salary expectation IS on record — so this job
      // order's salary absence is job-specific, and the generic note stands.
      candidate_salary: { amount: 3000, currency: "SGD", period: "month" },
    });

    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    expect(
      await screen.findByText(/No comparable salary: one side is missing, or the currencies differ/),
    ).toBeTruthy();
  });

  it("says so when nothing scored high enough", async () => {
    jobsRun = jobsView([], { considered: 2, scored: 0 });

    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    expect(
      await screen.findByText(/last run found nothing worth showing/),
    ).toBeTruthy();
    expect(screen.getByText(/2 visible job orders were examined/)).toBeTruthy();
  });

  it("shows the server's message when the run fails", async () => {
    jobsRunError = "We could not reach the server.";

    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    expect(await screen.findByText("We could not reach the server.")).toBeTruthy();
  });

  it("shows the shortlist count on the Jobs tab once a run exists", async () => {
    jobsRun = runView({}, 5);
    render(panel(candidate()));
    // Before the run: plain "Jobs", nothing to count.
    expect(screen.getByRole("tab", { name: "Jobs" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    // After the run: the count tells the recruiter the saved shortlist is
    // worth opening — the awareness the label exists to give.
    expect(await screen.findByRole("tab", { name: "Jobs (5)" })).toBeTruthy();
  });

  it("keeps the plain Jobs label when the last run found nothing", async () => {
    jobsRun = jobsView([], { considered: 2, scored: 0 });
    render(panel(candidate()));
    fireEvent.click(screen.getByRole("button", { name: "Find Job" }));

    expect(await screen.findByText(/last run found nothing/)).toBeTruthy();
    // A "(0)" would promise an empty tab the recruiter is already looking at.
    expect(screen.getByRole("tab", { name: "Jobs" })).toBeTruthy();
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

  it("defaults to the Assessment tab when an analysis result exists", async () => {
    // A stored, finished analysis: the sharp headline read is what the
    // recruiter opens the modal for, so Assessment — not Details — is the tab
    // they land on.
    intelView = {
      id: "i-1",
      state: "done",
      failure_reason: null,
      analysed_at: "2026-08-25T10:00:00Z",
      intelligence: {
        work: { roles: [], education: [] },
        assessment: {
          headline: "A capable operations manager",
          summary: "Summary",
          work_level: "",
          decision_authority: "",
          scarce_capabilities: [],
          depreciated_capabilities: [],
          unproven_claims: [],
          ai_exposure: "",
          hire_readiness: "",
          value_trajectory: "",
        },
      },
    };
    render(panel(candidate()));

        // The effect runs after the fetch lands; wait for the Assessment tab to
    // become selected. The default would be Details, so seeing Assessment
    // selected proves the analysis-defaulting logic fired. Use `waitFor` since
    // the tab element exists immediately (the tabs are always rendered) and
    // only its `aria-selected` attribute changes.
    await waitFor(() => {
      const tab = screen.getByRole("tab", { name: "Assessment" });
      expect(tab.getAttribute("aria-selected")).toBe("true");
    });
  });

  it("stays on the tab the recruiter chose when analysis is absent", async () => {
    // No analysis exists: the modal opens on Details, and a manual choice of
    // another tab stays put (the analysis defaulting only applies when a
    // result actually arrives — with none, nothing redirects).
    intelView = { intelligence: null };
    render(panel(candidate()));
    fireEvent.click(await screen.findByRole("tab", { name: "Work" }));

    expect(screen.getByRole("tab", { name: "Work" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tab", { name: "Assessment" }).getAttribute("aria-selected")).toBe(
      "false",
    );
  });
});
