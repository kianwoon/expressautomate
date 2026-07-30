import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Me } from "../auth";
import { JobOrders } from "./job-orders";
import type { Opportunity } from "./opportunities";

/**
 * A job order taken over the phone, typed in by the person who took it.
 *
 * Not every vacancy arrives as an email, and the ones that do not are the ones
 * a recruiter is holding in their head while the client is still talking. Four
 * rules are pinned here, and each is one someone would otherwise re-decide:
 *
 *  1. The form sends no assignee. The server assigns the row to its creator —
 *     you typed it in, so it is yours — and a form that named one would be a
 *     second opinion about that, most likely the client account holder's.
 *  2. An empty client is ordinary, not a validation failure. A company nobody
 *     has recorded yet is precisely why `client_id` is nullable.
 *  3. The client field searches as you type. Clients are paginated and an
 *     agency accumulates hundreds; the 3-50 member list is preloaded and this
 *     one cannot be, so it is not the same component.
 *  4. The new row appears without a reload, because the recruiter is watching
 *     for confirmation that what they typed landed.
 *
 * `./events` is stubbed because the live stream opens an `EventSource`, which
 * happy-dom does not have.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

vi.mock("./job-orders-sourcing", () => ({ Shortlist: () => null }));

vi.mock("../events", () => ({
  useLive: () => {},
  useLiveStatus: () => "live",
}));

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-07-30T00:00:00Z",
    company_name_raw: "Acme",
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
    assigned_user_id: null,
    assignee_name: null,
    client_id: null,
    source: "pipeline",
    shared_with_me: false,
    ...overrides,
  } as Opportunity;
}

function me(): Me {
  return {
    user: {
      id: "u-1",
      email: "recruiter@agency.sg",
      display_name: "Recruiter",
      preferred_name: null,
      role: "member",
    },
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
  } as unknown as Me;
}

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body, text: async () => "" };
}

const CREATED = opportunity({
  id: "op-new",
  job_title_raw: "Warehouse assistant",
  company_name_raw: "Sunrise Logistics",
  source: "manual",
  assigned_user_id: "u-1",
  assignee_name: "Recruiter",
});

type Call = { url: string; init?: RequestInit };

/** Routes by URL and method: the list, the client search, the create, and the
 *  read-back of the row that was just created. */
function mockFetch() {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.includes("/api/clients")) {
      return jsonResponse({
        items: [{ id: "cl-1", name: "Sunrise Logistics" }],
        total: 1,
        limit: 8,
        offset: 0,
        counts: { all: 1 },
      });
    }
    if (url.includes("/api/opportunities") && method === "POST") {
      return jsonResponse({ id: "op-new" }, 201);
    }
    if (url.match(/\/api\/opportunities\/[^/?]+$/)) return jsonResponse(CREATED);
    return jsonResponse({
      items: [opportunity()],
      total: 1,
      limit: 25,
      offset: 0,
      counts: { all: 1, new: 1, needs_review: 0, reviewed: 0 },
    });
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
  return fetchMock;
}

/** The calls this suite asserts on are the writes; the list polls in between
 *  and would otherwise be "the last call" every time. */
function calls(fetchMock: ReturnType<typeof mockFetch>): Call[] {
  return fetchMock.mock.calls.map(([url, init]) => ({ url: url as string, init }));
}

function lastBody(fetchMock: ReturnType<typeof mockFetch>): Record<string, unknown> {
  const post = calls(fetchMock)
    .filter((c) => (c.init?.method ?? "GET") === "POST")
    .pop();
  if (!post?.init?.body) throw new Error("no POST was made");
  return JSON.parse(post.init.body as string) as Record<string, unknown>;
}

function lastUrl(fetchMock: ReturnType<typeof mockFetch>): string {
  const last = calls(fetchMock).pop();
  return last?.url ?? "";
}

async function openForm() {
  render(<JobOrders me={me()} />);
  await screen.findAllByText("Accountant");
  fireEvent.click(screen.getByRole("button", { name: "New job order" }));
  await screen.findByRole("dialog");
}

async function fillAndSave(): Promise<void> {
  fireEvent.change(screen.getByLabelText("Job title"), {
    target: { value: "Warehouse assistant" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save job order" }));
}

describe("typing in a job order", () => {
  let fetchMock: ReturnType<typeof mockFetch>;

  beforeEach(() => {
    fetchMock = mockFetch();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("posts a manual job order", async () => {
    await openForm();
    await fillAndSave();
    await waitFor(() =>
      expect(lastBody(fetchMock)).toMatchObject({ job_title_raw: "Warehouse assistant" }),
    );
  });

  it("lands assigned to its creator", async () => {
    // You typed it in, it is yours - the API does this; the test pins that the
    // form does not try to set an assignee itself.
    await openForm();
    await fillAndSave();
    await waitFor(() => expect(lastBody(fetchMock)).toBeTruthy());
    expect(lastBody(fetchMock)).not.toHaveProperty("assigned_user_id");
  });

  it("allows an empty client", async () => {
    // A job order taken over the phone from a company you have not recorded yet
    // has no client, and client_id is nullable precisely for that.
    await openForm();
    await fillAndSave();
    await waitFor(() => expect(lastBody(fetchMock)).toBeTruthy());
    expect(lastBody(fetchMock).client_id).toBeNull();
  });

  it("searches clients as you type rather than preloading them", async () => {
    // Clients are paginated and an agency accumulates hundreds; members are
    // 3-50 and load once. The two pickers are not the same component.
    await openForm();
    fireEvent.change(screen.getByLabelText("Client"), { target: { value: "sun" } });
    await waitFor(() => expect(lastUrl(fetchMock)).toContain("q=sun"));
  });

  it("shows the new job order in the list without a reload", async () => {
    await openForm();
    await fillAndSave();
    await waitFor(() => expect(screen.getAllByText("Warehouse assistant").length).toBeGreaterThan(0));
  });
});
