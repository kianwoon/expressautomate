import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Candidate } from "../candidates";
import { clearSignedUrlCache } from "../signed-url-cache";
import { CandidateAvatar } from "./candidate-avatar";

/**
 * Two performance properties, pinned as behaviour rather than as timings.
 *
 * 1. A candidate with no photo — which is most of them — costs no request at
 *    all. `avatar_key` is already on the record, so asking the API is a 404
 *    spent confirming what the caller already knew, and until it came back the
 *    circle sat announcing itself as loading. `ClientLogo` always did this;
 *    the avatar did not.
 * 2. Re-opening the same panel reuses the SAME URL string. Not merely "makes
 *    no second API call" — the string has to be identical, because that is
 *    what lets the browser reuse the image bytes instead of downloading them
 *    again under a freshly signed URL.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on.
 */

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
  clearSignedUrlCache();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("CandidateAvatar", () => {
  it("shows the initials and fires no request when the record says there is no photo", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<CandidateAvatar row={candidate()} onChanged={() => {}} />);

    expect(await screen.findByLabelText("Wei Ming Tan has no photo")).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reuses the same signed URL when the panel is re-opened", async () => {
    let minted = 0;
    const fetchMock = vi.fn().mockImplementation(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ url: `https://r2.test/avatar?sig=${(minted += 1)}`, expires_in: 300 }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const row = candidate({
      avatar_key: "t-1/candidates/cand-1/avatar",
      avatar_updated_at: "2026-08-03T00:00:00Z",
    });

    const first = render(<CandidateAvatar row={row} onChanged={() => {}} />);
    const firstSrc = await waitFor(() => {
      const img = document.querySelector("img.ca-photo") as HTMLImageElement | null;
      expect(img).toBeTruthy();
      return img!.src;
    });
    first.unmount();

    render(<CandidateAvatar row={row} onChanged={() => {}} />);
    const secondSrc = await waitFor(() => {
      const img = document.querySelector("img.ca-photo") as HTMLImageElement | null;
      expect(img).toBeTruthy();
      return img!.src;
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(secondSrc).toBe(firstSrc);
  });
});
