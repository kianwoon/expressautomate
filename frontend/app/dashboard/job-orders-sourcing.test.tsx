import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Client } from "./clients";
import { Shortlist } from "./job-orders-sourcing";
import type { Opportunity } from "./opportunities";
import type { SourcingMatch, SourcingRun, SourcingView } from "./sourcing";

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
const getSourcingRun = vi.fn();
const listSourcingRuns = vi.fn();
const namesFor = vi.fn<(ids: string[], known: ReadonlyMap<string, string>) => Promise<Map<string, string>>>(
  async () => new Map(),
);

vi.mock("./sourcing", async () => {
  const actual = await vi.importActual<typeof import("./sourcing")>("./sourcing");
  return {
    ...actual,
    getSourcing: (...args: unknown[]) => getSourcing(...args),
    getSourcingRun: (...args: unknown[]) => getSourcingRun(...args),
    listSourcingRuns: (...args: unknown[]) => listSourcingRuns(...args),
    namesFor: (ids: string[], known: ReadonlyMap<string, string>) => namesFor(ids, known),
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
    sex_prefilter_applied: false,
    sex_prefilter_value: null,
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

function match(overrides: Partial<SourcingMatch> = {}): SourcingMatch {
  return {
    candidate_id: "cand-1",
    score: "0.8200",
    reasons: [{ name: "skills", weight: "40", raw: "8", contribution: "32", note: null }],
    explanation: "Strong match on skills and recent tenure.",
    explanation_evidence: "5 years as an accountant at a mid-size firm.",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  getClient.mockReset();
  getSourcing.mockReset();
  getSourcingRun.mockReset();
  listSourcingRuns.mockReset();
  namesFor.mockReset();
  namesFor.mockResolvedValue(new Map());
  // The panel lists its run history on mount; an empty history is the default
  // every existing test expects. Set here AND in beforeEach so the very first
  // test of a file — which runs before any afterEach has — sees it too.
  listSourcingRuns.mockResolvedValue([]);
});

beforeEach(() => {
  listSourcingRuns.mockResolvedValue([]);
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

describe("Shortlist run history", () => {
  it("shows the history dropdown once there are two runs, newest first", async () => {
    getSourcing.mockResolvedValue(view({ run: run({ id: "run-2" }), matches: [] }));
    listSourcingRuns.mockResolvedValue([
      run({ id: "run-2", created_at: "2026-07-31T10:00:00Z" }),
      run({ id: "run-1", created_at: "2026-07-30T10:00:00Z" }),
    ]);

    render(<Shortlist row={opportunity()} />);

    const select = (await screen.findByRole("combobox")) as HTMLSelectElement;
    // The latest run leads the history and is the default selection.
    expect(select).toBeTruthy();
    expect(select.value).toBe("run-2");
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["run-2", "run-1"]);
  });

  it("loads an earlier run by id when selected, and returns to the latest", async () => {
    getSourcing.mockResolvedValue(view({ run: run({ id: "run-2" }), matches: [] }));
    getSourcingRun.mockResolvedValue(
      view({
        run: run({ id: "run-1", candidates_considered: 5 }),
        matches: [match({ candidate_id: "cand-old" })],
      }),
    );
    listSourcingRuns.mockResolvedValue([
      run({ id: "run-2", created_at: "2026-07-31T10:00:00Z" }),
      run({ id: "run-1", created_at: "2026-07-30T10:00:00Z" }),
    ]);
    namesFor.mockResolvedValue(new Map([["cand-old", "Old Candidate"]]));

    render(<Shortlist row={opportunity()} />);

    const select = (await screen.findByRole("combobox")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "run-1" } });

    await waitFor(() => expect(getSourcingRun).toHaveBeenCalledWith("op-1", "run-1"));
    await screen.findByText("Old Candidate");

    // Switching back to the newest run fetches the latest again.
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "run-2" } });
    await waitFor(() => expect(getSourcing).toHaveBeenCalledWith("op-1"));
  });

  it("keeps the history dropdown hidden for a single run", async () => {
    getSourcing.mockResolvedValue(view({ run: run({ id: "run-1" }), matches: [] }));
    listSourcingRuns.mockResolvedValue([run({ id: "run-1" })]);

    render(<Shortlist row={opportunity()} />);

    await waitFor(() => expect(getSourcing).toHaveBeenCalled());
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});

describe("Shortlist server-reported submissions", () => {
  it("renders Submitted for a match the server already marks submitted", async () => {
    getSourcing.mockResolvedValue(
      view({
        run: run({ client_id: "cl-1" }),
        matches: [
          match({ candidate_id: "cand-1", submitted: true }),
          match({ candidate_id: "cand-2", submitted: false }),
        ],
      }),
    );
    getClient.mockResolvedValue(client());
    namesFor.mockResolvedValue(
      new Map([
        ["cand-1", "Jane Tan"],
        ["cand-2", "Bob Lee"],
      ]),
    );

    render(<Shortlist row={opportunity()} />);

    await screen.findByText("Jane Tan");
    // Exactly one row is "Submitted": the one the server flags. The other
    // keeps its action, so a colleague's submission renders truthfully even
    // though this session never clicked.
    expect(screen.getAllByText("Submitted").length).toBe(1);
    expect(screen.getByText("Mark Bob Lee submitted")).toBeTruthy();
  });
});

describe("Shortlist score display", () => {
  it("renders the overall score as a whole percentage", async () => {
    getSourcing.mockResolvedValue(view({ run: run(), matches: [match({ score: "0.3018" })] }));
    namesFor.mockResolvedValue(new Map([["cand-1", "Jane Tan"]]));

    render(<Shortlist row={opportunity()} />);

    await screen.findByText("Jane Tan");
    expect(screen.getByText("30%")).toBeTruthy();
    // The stored string itself is never shown next to the percentage.
    expect(screen.queryByText("0.3018")).toBeNull();
  });

  it("rounds each scored component to a whole percentage of its weight, and an absent one in words", async () => {
    getSourcing.mockResolvedValue(
      view({
        run: run(),
        matches: [
          match({
            score: "0.8200",
            reasons: [
              { name: "skills", weight: "3.0", raw: "0.5368", contribution: "1.6104", note: null },
              {
                name: "salary",
                weight: "2.0",
                raw: null,
                contribution: null,
                note: "No comparable salary: one side is missing, or the currencies differ.",
              },
            ],
          }),
        ],
      }),
    );
    namesFor.mockResolvedValue(new Map([["cand-1", "Jane Tan"]]));

    render(<Shortlist row={opportunity()} />);

    await screen.findByText("Jane Tan");
    expect(screen.getByText("82%")).toBeTruthy();
    // 1.6104 of 3.0 → 53.68% → 54%, not "1.6104 of 3.0".
    expect(screen.getByText("54%")).toBeTruthy();
    expect(screen.queryByText("1.6104 of 3.0")).toBeNull();
    // A component with nothing to compare still says so in words, never "0%".
    expect(
      screen.getByText("No comparable salary: one side is missing, or the currencies differ."),
    ).toBeTruthy();
    expect(screen.queryByText("0%")).toBeNull();
  });
});

describe("Shortlist redacted matches", () => {
  it("renders the masked name and holder for a redacted match, never the raw id", async () => {
    getSourcing.mockResolvedValue(
      view({
        run: run(),
        matches: [
          match({
            candidate_id: "cand-private",
            visible: false,
            full_name: "Wei Ming T.",
            held_by: "Sarah Lim",
            can_request_access: true,
            reasons: null,
            explanation: null,
            explanation_evidence: null,
          }),
        ],
      }),
    );

    render(<Shortlist row={opportunity()} />);

    await waitFor(() => expect(screen.getAllByText("Wei Ming T.").length).toBeGreaterThan(0));
    expect(
      screen.getByText((_, el) => el?.tagName === "P" && el.textContent === "Held by Sarah Lim."),
    ).toBeTruthy();
    expect(screen.queryByText("cand-private")).toBeNull();
    // The redacted id is never sent to the by-id candidate lookup — that is
    // exactly the request that used to 404 and fall back to the raw UUID.
    await waitFor(() => expect(namesFor).toHaveBeenCalled());
    expect(namesFor.mock.calls[0][0]).toEqual([]);
  });

  it("still renders explanation and reasons unchanged for a visible match", async () => {
    getSourcing.mockResolvedValue(
      view({
        run: run(),
        matches: [match({ candidate_id: "cand-visible" })],
      }),
    );
    namesFor.mockResolvedValue(new Map([["cand-visible", "Jane Tan"]]));

    render(<Shortlist row={opportunity()} />);

    await screen.findByText("Jane Tan");
    expect(screen.getByText("Strong match on skills and recent tenure.")).toBeTruthy();
    expect(screen.getByText("5 years as an accountant at a mid-size firm.")).toBeTruthy();
    expect(namesFor.mock.calls[0][0]).toEqual(["cand-visible"]);
  });

  it("shows the request-access affordance only when can_request_access is true", async () => {
    getSourcing.mockResolvedValue(
      view({
        run: run(),
        matches: [
          match({
            candidate_id: "cand-no-request",
            visible: false,
            full_name: "K. Osman",
            held_by: "Rafi",
            can_request_access: false,
            reasons: null,
            explanation: null,
            explanation_evidence: null,
          }),
        ],
      }),
    );

    render(<Shortlist row={opportunity()} />);

    await screen.findByText("K. Osman");
    expect(
      screen.getByText((_, el) => el?.tagName === "P" && el.textContent === "Held by Rafi."),
    ).toBeTruthy();
    expect(screen.queryByText("Request access")).toBeNull();
  });
});
