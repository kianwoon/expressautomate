import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PlacementForm } from "./job-order-placement";
import type { Opportunity } from "./opportunities";

/**
 * The placement form, against the routes the backend actually serves.
 *
 * This file exists because the form shipped talking to a `PATCH
 * /api/opportunities/{id}` that has never existed, and every test around it
 * passed: the fetch stubs of the day answered any URL with a plausible body,
 * so a call to nothing looked exactly like a call that worked.
 *
 * So the stub here refuses anything it was not told about, and each test names
 * the exact route and method it expects. A form that goes back to one combined
 * PATCH fails on the first assertion rather than on a production screen.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

type Call = { url: string; method: string; body: unknown };

function row(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-07-30T00:00:00Z",
    company_name_raw: "Sunrise Care Pte Ltd",
    job_title_raw: "Carer",
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
    client_name: null,
    source: "pipeline",
    shared_with_me: false,
    ...overrides,
  } as Opportunity;
}

/**
 * A stub that answers only the routes it was handed, and 404s the rest —
 * which is what the real server does with a route that does not exist.
 */
function stubFetch(routes: Record<string, () => Response>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const key = `${method} ${url}`;
    calls.push({
      url,
      method,
      body: init?.body ? JSON.parse(String(init.body)) : null,
    });
    const handler = routes[key];
    if (!handler) {
      return Promise.resolve(
        new Response(JSON.stringify({ detail: "Not Found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }
    return Promise.resolve(handler());
  });
  return calls;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const PLACEMENT = "POST /api/opportunities/op-1/placement-type";
const REQUIREMENT = "POST /api/opportunities/op-1/occupational-requirement";
const READ_BACK = "GET /api/opportunities/op-1";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the placement form", () => {
  it("posts the placement type to the route that sets it", async () => {
    const saved = row({ placement_type: "mdw_work_permit" });
    const calls = stubFetch({
      [PLACEMENT]: () => json({ id: "op-1", placement_type: "mdw_work_permit" }),
      [READ_BACK]: () => json(saved),
    });
    const onSaved = vi.fn();
    render(<PlacementForm row={row()} onSaved={onSaved} />);

    fireEvent.change(screen.getByDisplayValue("Not set"), {
      target: { value: "mdw_work_permit" },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(calls.map((c) => `${c.method} ${c.url}`)).toContain(PLACEMENT);
    expect(calls.find((c) => c.url.endsWith("/placement-type"))?.body).toEqual({
      placement_type: "mdw_work_permit",
    });
    // The bug in one line: nothing may PATCH the row itself.
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
    expect(onSaved).toHaveBeenCalledWith(saved);
  });

  it("sends a sex requirement and its reason together, to their own route", async () => {
    const saved = row({
      sex_requirement: "female",
      sex_requirement_reason: "Intimate personal care.",
    });
    const calls = stubFetch({
      [REQUIREMENT]: () => json({ id: "op-1" }),
      [READ_BACK]: () => json(saved),
    });
    render(<PlacementForm row={row()} onSaved={vi.fn()} />);

    fireEvent.change(screen.getByDisplayValue("None"), { target: { value: "female" } });
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Intimate personal care." },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() =>
      expect(calls.some((c) => c.url.endsWith("/occupational-requirement"))).toBe(true),
    );
    expect(calls.find((c) => c.url.endsWith("/occupational-requirement"))?.body).toEqual({
      sex_requirement: "female",
      sex_requirement_reason: "Intimate personal care.",
    });
    // The placement type did not change, so it is not written — and its audit
    // columns are not stamped with a decision nobody made.
    expect(calls.some((c) => c.url.endsWith("/placement-type"))).toBe(false);
  });

  it("keeps what did save when the second write fails, and says so", async () => {
    // Placement type lands; the sex requirement is refused. The row on the
    // server now has one of the two, and that is what the panel must show.
    const partial = row({ placement_type: "mdw_work_permit" });
    stubFetch({
      [PLACEMENT]: () => json({ id: "op-1", placement_type: "mdw_work_permit" }),
      [REQUIREMENT]: () => json({ detail: "A reason has to be on record." }, 422),
      [READ_BACK]: () => json(partial),
    });
    const onSaved = vi.fn();
    render(<PlacementForm row={row()} onSaved={onSaved} />);

    fireEvent.change(screen.getByDisplayValue("Not set"), {
      target: { value: "mdw_work_permit" },
    });
    fireEvent.change(screen.getByDisplayValue("None"), { target: { value: "female" } });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Because." } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("reason"));
    // The read-back still ran, so the panel is not left showing a sex
    // requirement the server refused.
    expect(onSaved).toHaveBeenCalledWith(partial);
  });

  it("resyncs its own selects to the server's values after a partial save", async () => {
    // Same refusal as above, but this time assert on the form's own fields,
    // not just what got handed up to the panel. Before the fix, `useState`
    // only read `row` on first mount, so these selects kept showing the
    // refused "female" / "Because." instead of falling back to what the
    // server actually holds (no sex requirement at all).
    const partial = row({ placement_type: "mdw_work_permit" });
    stubFetch({
      [PLACEMENT]: () => json({ id: "op-1", placement_type: "mdw_work_permit" }),
      [REQUIREMENT]: () => json({ detail: "A reason has to be on record." }, 422),
      [READ_BACK]: () => json(partial),
    });
    render(<PlacementForm row={row()} onSaved={vi.fn()} />);

    fireEvent.change(screen.getByDisplayValue("Not set"), {
      target: { value: "mdw_work_permit" },
    });
    fireEvent.change(screen.getByDisplayValue("None"), { target: { value: "female" } });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Because." } });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("reason"));

    // The placement type did save, so the select keeps showing it.
    expect(screen.getByDisplayValue("MDW Work Permit")).toBeTruthy();
    // The requirement was refused, so the select and reason box must fall
    // back to the server's values (none), not keep showing what was typed.
    expect(screen.getByDisplayValue("None")).toBeTruthy();
    expect(screen.queryByDisplayValue("Because.")).toBe(null);
  });

  it("shows an error when the read-back route is missing", async () => {
    // Everything 404s — the state the form shipped in. A form that reports
    // success here is a form that cannot tell a write from a wrong URL.
    stubFetch({});
    const onSaved = vi.fn();
    render(<PlacementForm row={row()} onSaved={onSaved} />);

    fireEvent.change(screen.getByDisplayValue("Not set"), {
      target: { value: "s_pass" },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => expect(screen.queryByRole("alert")).not.toBe(null));
    expect(onSaved).not.toHaveBeenCalled();
  });
});
