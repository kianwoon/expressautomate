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
