import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Candidate } from "../candidates";
import { CandidateForm } from "./candidate-form";

/**
 * "Not recorded" has to survive the round trip.
 *
 * An unrecorded date of birth makes the Work Permit age check `unknown`,
 * which *keeps* a candidate in the eligible list — only a fact that is known
 * and fails removes anyone. So a recruiter who picked the wrong sex, or was
 * told wrong, must be able to get back to knowing nothing. If the form can
 * only ever set these fields, the first guess is permanent, and a guess is
 * what the eligibility rules then treat as fact.
 *
 * The backend half is pinned by `test_patch_updates_and_clears_each_field`.
 * This is the half that decides whether the PATCH ever carries the null.
 */

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

function row(overrides: Partial<Candidate> = {}): Candidate {
  return {
    id: "cand-1",
    full_name: "Tan Hui Ling",
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
    avatar_key: null,
    avatar_updated_at: null,
    ...overrides,
  } as Candidate;
}

/** The JSON body of the last fetch, which is the PATCH under test. */
function lastBody(fetchMock: ReturnType<typeof vi.fn>): Record<string, unknown> {
  const calls = fetchMock.mock.calls;
  return JSON.parse((calls[calls.length - 1][1] as RequestInit).body as string);
}

afterEach(() => {
  // Explicit, because Testing Library only registers its own auto-cleanup
  // when Vitest's globals are enabled, and this config does not enable them.
  // Without it each render stacks on the last and `getByLabelText` starts
  // finding a field per test — which reads as an ambiguous label in the form
  // rather than as leftover DOM.
  cleanup();
  vi.unstubAllGlobals();
});

describe("not recorded is a value the form can send, not just one it can show", () => {
  it("clearing sex back to Not recorded sends an explicit null", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(row({ sex: null })));
    vi.stubGlobal("fetch", fetchMock);

    render(<CandidateForm row={row({ sex: "female" })} onDone={() => {}} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText(/^Sex$/), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // `null`, not absent. The PATCH dumps with `exclude_unset=True`, so an
    // omitted key means "leave it alone" — the difference between clearing a
    // field and never touching it is exactly this line.
    const body = lastBody(fetchMock);
    expect("sex" in body).toBe(true);
    expect(body.sex).toBeNull();
  });

  it("a field the recruiter never touched is not sent at all", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(row()));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CandidateForm
        row={row({ sex: "female", nationality: "PH" })}
        onDone={() => {}}
        onCancel={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText(/^Location$/), { target: { value: "Singapore" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // Sending every field would mark each one an override, freezing it against
    // every later import — so an untouched field must stay out of the body.
    const body = lastBody(fetchMock);
    expect(body.location).toBe("Singapore");
    expect("sex" in body).toBe(false);
    expect("nationality" in body).toBe(false);
  });

  it("race detail does not outlive the race it described", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(row()));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CandidateForm
        row={row({ race: "others", race_detail: "Eurasian" })}
        onDone={() => {}}
        onCancel={() => {}}
      />,
    );

    fireEvent.change(screen.getByLabelText(/^Race$/), { target: { value: "chinese" } });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // Otherwise the record says "Chinese" and "Eurasian" at once — two claims
    // about one person, disagreeing.
    const body = lastBody(fetchMock);
    expect(body.race).toBe("chinese");
    expect(body.race_detail).toBeNull();
  });
});

describe("every free-text field carries an example placeholder", () => {
  // A blank text box gives a recruiter no sense of the expected shape or
  // length. Each field below must show a concrete example — pinned here so a
  // future edit that drops one is caught by the suite rather than by a
  // recruiter staring at an empty box. Date inputs and the two selects that
  // already open on a real "Not recorded" default are deliberately excluded:
  // a placeholder on a native date picker is not shown by most browsers, and
  // a select with a chosen default has nothing for a placeholder to fill.
  it("shows an example in every text, number and textarea field", () => {
    render(<CandidateForm row={null} onDone={() => {}} onCancel={() => {}} />);

    const expected: [string, string][] = [
      ["Full name *", "Tan Wei Ming"],
      ["Email", "weiming.tan@gmail.com"],
      ["Phone", "+65 9123 4567"],
      ["Current title", "Warehouse assistant"],
      ["Current employer", "Acme Logistics Pte Ltd"],
      ["Location", "Tuas"],
      ["Years of experience", "3"],
      ["Last drawn salary", "2500"],
      ["Last drawn salary currency", "SGD"],
      ["Last drawn salary period", "month"],
      ["Expected salary", "2800"],
      ["Expected salary currency", "SGD"],
      ["Expected salary period", "month"],
      ["Notice period", "2 weeks"],
      ["Employment type", "Full-time"],
      ["Skills (comma separated)", "forklift operation, inventory management, WMS"],
      ["Years of formal education", "10"],
    ];
    expected.forEach(([label, placeholder]) => {
      expect(screen.getByLabelText(label).getAttribute("placeholder")).toBe(placeholder);
    });

    // Nationality's placeholder doubles as format guidance ("Two-letter
    // country code, e.g. PH") because the field is validated to that shape —
    // still an example, just pinned separately from the plain-example list
    // above. `getByLabelText` cannot resolve this one reliably because the
    // input carries a `list` attribute pointing at its `<datalist>`, so it is
    // located by its placeholder instead.
    expect(screen.getByPlaceholderText("Two-letter country code, e.g. PH")).toBeTruthy();

    expect(screen.getByLabelText(/^Notes$/).getAttribute("placeholder")).toBe(
      "Available for immediate start; prefers day shift",
    );
  });

  it("shows an example for race detail once Others is picked", () => {
    render(<CandidateForm row={null} onDone={() => {}} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText(/^Race$/), { target: { value: "others" } });

    expect(screen.getByLabelText("Race detail").getAttribute("placeholder")).toBe("Eurasian");
  });
});
