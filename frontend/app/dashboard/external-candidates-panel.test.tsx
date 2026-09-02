import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ExternalCandidatesStage,
  type ExternalPanelState,
} from "./external-candidates-panel";
import type {
  ExternalCandidate,
  ExternalSearchResults,
  ExternalTaskStatus,
} from "./external-candidates";

/**
 * The External Candidates panel renders what the hook produced — so each test
 * hands it one state and asserts the sentences and controls a recruiter sees.
 * The hook itself (`useExternalCandidates`) is deliberately not rendered
 * here: its network loop is thin over `startExternalSearch` and friends, and
 * faking timers against it would test the interval, not the modal.
 *
 * allow-hardcode: the strings below are verbatim copies of user-facing copy
 * being asserted on.
 */

const PLAN = { hasSearchPlan: true } satisfies ExternalPanelState;

function candidate(overrides: Partial<ExternalCandidate> = {}): ExternalCandidate {
  return {
    id: "c-1",
    title: "Chean Wei Yap",
    subtitle: "Hands-On Software Architect",
    location: "Singapore, Singapore",
    source: "linkedin_people",
    source_url: "https://www.linkedin.com/in/yap-chean-wei/",
    match_score: 90.4,
    match_reason: "20+ yrs, Singapore-based, hands-on architect",
    evidence: [],
    gaps: [],
    recommended_action: null,
    status: "new",
    summary: null,
    skills: ["Microservices", "AWS", "Java"],
    experience: null,
    education: null,
    certifications: null,
    credibility: null,
    ...overrides,
  };
}

function results(
  overrides: Partial<ExternalSearchResults> = {},
): ExternalSearchResults {
  return {
    status: "ok",
    task_id: "task-1",
    task_status: "completed",
    summary: "10 ranked results",
    results: [candidate()],
    message: null,
    ...overrides,
  };
}

function panel(overrides: {
  state?: ExternalPanelState;
  starting?: boolean;
  startError?: string | null;
  taskStatus?: ExternalTaskStatus;
  taskError?: string | null;
  results?: ExternalSearchResults | null;
  resultsError?: string | null;
  onFind?: () => void;
} = {}) {
  return render(
    <ExternalCandidatesStage
      state={overrides.state ?? PLAN}
      starting={overrides.starting ?? false}
      startError={overrides.startError ?? null}
      taskStatus={overrides.taskStatus ?? null}
      taskError={overrides.taskError ?? null}
      results={overrides.results ?? null}
      resultsError={overrides.resultsError ?? null}
      onFind={overrides.onFind ?? (() => {})}
    />,
  );
}

afterEach(() => {
  cleanup();
});

describe("the Find External Candidates button", () => {
  it("offers the find button when a search plan exists", () => {
    panel();
    const button = screen.getByRole("button", { name: "Find External Candidates" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });

  it("disables the button and explains itself when there is no search plan", () => {
    panel({ state: { hasSearchPlan: false } });
    const button = screen.getByRole("button", { name: "Find External Candidates" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(
      screen.getByText(
        'No search plan yet. Use "Run analysis" at the top — external search uses the plan from the Search tab.',
      ),
    ).toBeTruthy();
  });

  it("fires onFind when clicked", () => {
    const onFind = vi.fn();
    panel({ onFind });
    fireEvent.click(screen.getByRole("button", { name: "Find External Candidates" }));
    expect(onFind).toHaveBeenCalledTimes(1);
  });

  it("shows Starting… and refuses a second click while the start is in flight", () => {
    panel({ starting: true });
    const button = screen.getByRole("button", { name: "Starting…" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows Searching… and stays disabled while the task runs", () => {
    panel({ taskStatus: "running" });
    const button = screen.getByRole("button", { name: "Searching…" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(
      screen.getByText("Searching external sources — this takes a minute or two."),
    ).toBeTruthy();
  });

  it("re-enables the button once the task has finished", () => {
    panel({ taskStatus: "completed", results: results() });
    const button = screen.getByRole("button", { name: "Find External Candidates" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("the sentences a recruiter is shown", () => {
  it("surfaces a start refusal in the server's words", () => {
    panel({ startError: "The external search service could not be reached." });
    expect(screen.getByRole("alert").textContent).toBe(
      "The external search service could not be reached.",
    );
  });

  it("surfaces a task failure in the career bot's words", () => {
    panel({ taskStatus: "failed", taskError: "The search agent stopped." });
    expect(screen.getByRole("alert").textContent).toBe("The search agent stopped.");
  });

  it("tells the recruiter a paused task needs a human, not a retry", () => {
    panel({ taskStatus: "paused" });
    expect(screen.getByRole("alert").textContent).toBe(
      "The search needs a human on the external service (a login or verification step). It is paused — try again later or contact support.",
    );
  });

  it("surfaces a results error", () => {
    panel({ resultsError: "The results could not be read just now." });
    expect(screen.getByRole("alert").textContent).toBe(
      "The results could not be read just now.",
    );
  });

  it("shows the summary alone when nothing matched", () => {
    panel({ taskStatus: "completed", results: results({ results: [], summary: null }) });
    expect(
      screen.getByText("No external candidates matched this search."),
    ).toBeTruthy();
    expect(screen.queryByTestId("jo-external-row")).toBeNull();
  });
});

describe("a rendered candidate row", () => {
  it("renders the ranked list with a traceable source link, score and reason", () => {
    panel({ taskStatus: "completed", results: results() });

    const row = screen.getByTestId("jo-external-row");
    const link = row.querySelector("a.jo-external-name") as HTMLAnchorElement;
    expect(link.textContent).toBe("Chean Wei Yap");
    expect(link.href).toBe("https://www.linkedin.com/in/yap-chean-wei/");
    expect(link.target).toBe("_blank");
    expect(link.rel).toContain("noreferrer");

    expect(row.querySelector(".jo-external-score")?.textContent).toBe("90");
    expect(row.textContent).toContain("20+ yrs, Singapore-based, hands-on architect");
  });

  it("rounds a fractional match score for display", () => {
    panel({
      taskStatus: "completed",
      results: results({ results: [candidate({ match_score: 87.6 })] }),
    });
    expect(screen.getByTestId("jo-external-row").querySelector(".jo-external-score")?.textContent).toBe(
      "88",
    );
  });

  it("renders a plain name when the source URL is missing", () => {
    panel({
      taskStatus: "completed",
      results: results({ results: [candidate({ source_url: null })] }),
    });
    const row = screen.getByTestId("jo-external-row");
    expect(row.querySelector("a.jo-external-name")).toBeNull();
    expect(row.querySelector(".jo-external-name")?.textContent).toBe("Chean Wei Yap");
  });

  it("shows the platform the candidate came from", () => {
    panel({ taskStatus: "completed", results: results() });
    const chip = screen.getByTestId("jo-external-platform");
    expect(chip.textContent).toBe("LinkedIn");
  });

  it("maps the jobstreet source id to its platform name", () => {
    panel({
      taskStatus: "completed",
      results: results({ results: [candidate({ source: "jobstreet - candidate" })] }),
    });
    expect(screen.getByTestId("jo-external-platform").textContent).toBe("JobStreet");
  });

  it("title-cases an unknown source id rather than inventing a platform", () => {
    panel({
      taskStatus: "completed",
      results: results({ results: [candidate({ source: "glassdoor_people" })] }),
    });
    expect(screen.getByTestId("jo-external-platform").textContent).toBe("Glassdoor People");
  });

  it("shows no platform chip when the result carries no source", () => {
    const row = { ...candidate(), source: undefined as unknown as string };
    panel({
      taskStatus: "completed",
      results: results({ results: [row] }),
    });
    expect(screen.queryByTestId("jo-external-platform")).toBeNull();
  });

  it("offers an explicit Open profile link when a source URL exists", () => {
    panel({ taskStatus: "completed", results: results() });
    const open = screen.getByTestId("jo-external-open") as HTMLAnchorElement;
    expect(open.textContent).toContain("Open profile");
    expect(open.href).toBe("https://www.linkedin.com/in/yap-chean-wei/");
    expect(open.target).toBe("_blank");
    expect(open.rel).toContain("noreferrer");
  });

  it("offers no Open profile link when the source URL is missing", () => {
    panel({
      taskStatus: "completed",
      results: results({ results: [candidate({ source_url: null })] }),
    });
    expect(screen.queryByTestId("jo-external-open")).toBeNull();
  });

  it("shows location and the first six skills as chips", () => {
    panel({
      taskStatus: "completed",
      results: results({
        results: [
          candidate({
            skills: ["A", "B", "C", "D", "E", "F", "G", "H"],
          }),
        ],
      }),
    });
    const chips = [
      ...screen.getByTestId("jo-external-row").querySelectorAll(".jo-external-chip"),
    ].map((el) => el.textContent);
    // The platform chip rides beside the name; the Open-profile link chip
    // leads the meta row — this fixture's row carries a source URL.
    expect(chips).toEqual([
      "LinkedIn",
      "Open profile ↗",
      "Singapore, Singapore",
      "A",
      "B",
      "C",
      "D",
      "E",
      "F",
    ]);
  });

  it("lists the gaps the search found", () => {
    panel({
      taskStatus: "completed",
      results: results({
        results: [candidate({ gaps: ["Kafka experience", "Banking domain"] })],
      }),
    });
    expect(screen.getByTestId("jo-external-row").textContent).toContain(
      "Missing: Kafka experience, Banking domain",
    );
  });

  it("shows credibility with its flags when the profile carries them", () => {
    panel({
      taskStatus: "completed",
      results: results({
        results: [
          candidate({
            credibility: {
              score: 88.2,
              title_inflation: 0,
              tenure_depth: 1,
              evidence_ratio: 0.8,
              flags: ["inflated titles"],
            },
          }),
        ],
      }),
    });
    expect(screen.getByTestId("jo-external-row").textContent).toContain(
      "Credibility 88 — flags: inflated titles",
    );
  });

  it("shows the recommended action when the search suggests one", () => {
    panel({
      taskStatus: "completed",
      results: results({
        results: [candidate({ recommended_action: "Reach out with the payments role." })],
      }),
    });
    expect(screen.getByTestId("jo-external-row").textContent).toContain(
      "Reach out with the payments role.",
    );
  });

  it("renders every row the search returned", () => {
    panel({
      taskStatus: "completed",
      results: results({
        results: [candidate(), candidate({ id: "c-2", title: "Second Person" })],
      }),
    });
    expect(screen.getAllByTestId("jo-external-row")).toHaveLength(2);
    expect(screen.getByTestId("jo-external-panel").textContent).toContain(
      "Second Person",
    );
  });
});
