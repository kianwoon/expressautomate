import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CandidateForm } from "./candidate-form";

/**
 * A candidate a colleague already holds is not a validation error.
 *
 * Before this, the 409 rendered as a red message above the form — and worse,
 * FastAPI nests this particular `detail` as an OBJECT, so the generic error
 * path handed React an object to render. Either way the recruiter is told to
 * change a value that is correct: the email they typed is that person's email,
 * and the only thing wrong is that they cannot see the record.
 *
 * What they need instead is the colleague's name and a way to ask. The name is
 * abbreviated and masked server-side (`abbreviate` in `candidate_matching.py`)
 * and is rendered exactly as given — expanding it is the disclosure the mask
 * exists to prevent.
 *
 * Driven through a stubbed `fetch` rather than a mocked `./candidates` module,
 * matching `candidate-form.test.tsx` beside it: that way the 409 parsing in
 * `writeError` is under test too, and a client that stopped reading the shape
 * would fail here rather than passing against its own mock.
 */

const COLLISION = {
  detail: {
    reason: "already_registered",
    candidate: { full_name: "Wei Ming T.", held_by: "Sarah Lim" },
    can_request_access: true,
  },
};

// Real shape since 2c6051f: `candidate.id` is now sent with the collision, so
// "Request access" has something to call.
const COLLISION_WITH_ID = {
  detail: {
    reason: "already_registered",
    candidate: { full_name: "Wei Ming T.", held_by: "Sarah Lim", id: "cand-123" },
    can_request_access: true,
  },
};

function conflict(body: unknown): Response {
  return {
    ok: false,
    status: 409,
    json: async () => body,
    clone() {
      return this as unknown as Response;
    },
  } as unknown as Response;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("a candidate a colleague already holds", () => {
  it("offers to ask rather than showing a validation error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(conflict(COLLISION)));

    render(<CandidateForm row={null} onDone={() => {}} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText(/full name/i), {
      target: { value: "Wei Ming Tan" },
    });
    fireEvent.change(screen.getByLabelText(/^email$/i), {
      target: { value: "weiming@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add candidate/i }));

    expect((await screen.findAllByText(/Sarah Lim/)).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /request access/i })).toBeTruthy();

    // The email field is not the problem, and marking it invalid tells the
    // recruiter to change a value that is correct.
    expect(screen.getByLabelText(/^email$/i).getAttribute("aria-invalid")).not.toBe("true");
  });

  it("renders the masked name as given, and never the raw one", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(conflict(COLLISION)));

    render(<CandidateForm row={null} onDone={() => {}} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText(/full name/i), {
      target: { value: "Wei Ming Tan" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add candidate/i }));

    await screen.findAllByText(/Sarah Lim/);
    expect(screen.getByText(/Wei Ming T\./)).toBeTruthy();
    // What the recruiter typed is on the form; the record's name is masked.
    // Finding it un-masked anywhere in the notice would mean something here
    // had "helpfully" expanded it.
    expect(screen.queryByText(/already registered.*Wei Ming Tan/i)).toBeNull();
  });

  it("still shows an ordinary 409 as an ordinary message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(conflict({ detail: "Already recorded as candidate abc" })),
    );

    render(<CandidateForm row={null} onDone={() => {}} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText(/full name/i), {
      target: { value: "Someone Else" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add candidate/i }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Already recorded"));
    expect(screen.queryByRole("button", { name: /request access/i })).toBeNull();
  });

  it("sends the request and says the owner was asked, once the id is present", async () => {
    const fetchMock = vi
      .fn()
      // First call: the collision itself.
      .mockResolvedValueOnce(conflict(COLLISION_WITH_ID))
      // Second call: POST /api/candidates/cand-123/access-requests.
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ id: "req-1" }),
      } as Response);
    vi.stubGlobal("fetch", fetchMock);

    render(<CandidateForm row={null} onDone={() => {}} onCancel={() => {}} />);

    fireEvent.change(screen.getByLabelText(/full name/i), {
      target: { value: "Wei Ming Tan" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add candidate/i }));

    const button = await screen.findByRole("button", { name: /request access/i });
    expect(button.hasAttribute("disabled")).toBe(false);

    fireEvent.click(button);

    await waitFor(() => expect(screen.getByText(/has been asked/i)).toBeTruthy());
    // A second click must not upsert a second pending row — the control that
    // sent the request is now the control saying it already did.
    const after = screen.getByRole("button", { name: /access requested/i });
    expect(after.hasAttribute("disabled")).toBe(true);

    const [, requestCall] = fetchMock.mock.calls;
    expect(String(requestCall[0])).toContain("cand-123");
    expect(String(requestCall[0])).toContain("access-requests");
  });
});
