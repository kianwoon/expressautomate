import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DetailPanel } from "./detail-panel";
import { resetMembers } from "./members";
import type { Opportunity } from "./opportunities";
import type { Me } from "../auth";

/**
 * Editing a job order's own fields from the detail panel.
 *
 * Four rules are pinned here, each one the thing a future change would
 * otherwise re-decide:
 *
 *  1. The Edit control appears only for the assignee (or the owner). A shared
 *     row is readable but not editable; an unassigned one is claimable first.
 *  2. Save sends only the fields that actually changed — sending everything
 *     would record an override for every field and freeze the row from later
 *     replays.
 *  3. The saved row (the full list-shaped payload the server answers with) is
 *     handed back up so the list and the panel agree immediately.
 *  4. A cleared field is sent as `null`, so the record returns to "not
 *     mentioned" rather than holding an empty string that renders as data.
 *
 * `./job-orders-sourcing`, the intelligence panels and the auth hook are
 * stubbed the same way the other panel tests stub them.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

vi.mock("./job-orders-sourcing", () => ({ Shortlist: () => null }));
vi.mock("./job-intelligence-panel", () => ({
  WorkStage: () => null,
  PersonStage: () => null,
  SearchStage: () => null,
}));
vi.mock("./job-intelligence", () => ({
  useJobIntelligence: () => ({
    phase: { status: "idle", view: { intelligence: null } },
    run: async () => {},
    starting: false,
    waiting: false,
    runError: null,
    view: null,
    analysis: null,
  }),
}));

let authState: Me | null = null;

vi.mock("../auth", () => ({
  useAuth: () =>
    authState ? { status: "signed-in", me: authState } : { status: "loading" },
}));

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
  } as unknown as Me;
}

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "op-1",
    received_datetime: "2026-07-30T00:00:00Z",
    company_name_raw: "Sunrise Care Pte Ltd",
    job_title_raw: "Carer",
    salary_raw: "$2,000/month",
    salary_min: 2000,
    salary_max: 2000,
    salary_currency: "SGD",
    salary_period: "month",
    working_hours_raw: "Mon–Fri",
    requirements: "At least 1 year.",
    job_description: "Care for an elderly client.",
    duration_raw: "6-month contract",
    location_raw: "West",
    employment_type: null,
    quality_state: "verified",
    review_status: "new",
    internet_message_id: null,
    graph_message_id: null,
    verified_fields: 0,
    total_fields: 0,
    placement_type: null,
    sex_requirement: null,
    sex_requirement_reason: null,
    assigned_user_id: "u-1",
    assignee_name: "Wei Kian",
    client_id: null,
    client_name: null,
    source: "pipeline",
    shared_with_me: false,
    ...overrides,
  } as Opportunity;
}

const SAVED = opportunity({ location_raw: "North", job_title_raw: "Senior carer" });

function panel(
  row: Opportunity,
  onEdited = vi.fn(),
) {
  return (
    <DetailPanel
      row={row}
      onClose={() => {}}
      onReview={async () => null}
      onClaim={async () => ({ ok: true }) as never}
      onAssign={async () => ({ ok: true }) as never}
      onClientSet={async () => ({ ok: true }) as never}
      onEdited={onEdited}
      onVanished={() => {}}
    />
  );
}

async function openEdit() {
  render(panel(opportunity()));
  fireEvent.click(await screen.findByRole("button", { name: "Edit details" }));
  await screen.findByLabelText("Job title");
}

const STAFF = [
  { id: "u-1", name: "Wei Kian", email: "wei@agency.sg", role: "recruiter" },
  { id: "u-2", name: "Priya", email: "priya@agency.sg", role: "recruiter" },
];

/** URL-aware: members come back as the array `useMembers` expects; the PATCH
 *  answers with the saved row, exactly as the server does. */
function mockFetch(onPatch = () => SAVED) {
  const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    if (String(url).includes("/api/members")) {
      return { ok: true, status: 200, json: async () => STAFF };
    }
    if (init?.method === "PATCH") {
      return { ok: true, status: 200, json: async () => onPatch() };
    }
    return { ok: true, status: 200, json: async () => SAVED };
  }) as unknown as ReturnType<typeof vi.fn>;
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  authState = me("u-1", "recruiter");
  resetMembers();
  mockFetch();
});

afterEach(() => {
  cleanup();
  resetMembers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("editing a job order's details", () => {
  it("offers Edit only to the assignee or the owner", async () => {
    authState = me("u-1", "recruiter");
    const { unmount } = render(panel(opportunity({ assigned_user_id: "u-1" })));
    expect(await screen.findByRole("button", { name: "Edit details" })).toBeTruthy();
    unmount();

    // A colleague who can see it (shared) cannot edit it.
    authState = me("u-2", "recruiter");
    const asBystander = render(panel(opportunity({ assigned_user_id: "u-1" })));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Edit details" })).toBeNull(),
    );
    asBystander.unmount();

    // The owner can edit anyone's.
    authState = me("u-9", "owner");
    render(panel(opportunity({ assigned_user_id: "u-1" })));
    expect(await screen.findByRole("button", { name: "Edit details" })).toBeTruthy();
  });

  it("hides Edit for an unassigned job order", async () => {
    authState = me("u-1", "recruiter");
    render(panel(opportunity({ assigned_user_id: null })));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Edit details" })).toBeNull(),
    );
  });

  it("replaces the read-only facts with editable fields", async () => {
    await openEdit();
    // The fields are editable inputs now, pre-filled from the row.
    expect((screen.getByLabelText("Job title") as HTMLInputElement).value).toBe("Carer");
    expect((screen.getByLabelText("Location") as HTMLInputElement).value).toBe("West");
    expect((screen.getByLabelText("Pay") as HTMLInputElement).value).toBe("$2,000/month");
    // The read-only rows are gone while editing.
    expect(screen.queryByText("Received")).toBeNull();
  });

  it("sends only the fields that changed", async () => {
    const fetchMock = mockFetch();
    await openEdit();
    fireEvent.change(screen.getByLabelText("Location"), { target: { value: "North" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const calls = fetchMock.mock.calls as Array<[unknown, RequestInit | undefined]>;
      const patch = calls.find(([, init]) => init?.method === "PATCH");
      expect(patch).toBeTruthy();
      const body = JSON.parse((patch as unknown as [unknown, RequestInit])[1].body as string);
      expect(body).toEqual({ location_raw: "North" });
    });
  });

  it("clears a field by sending null", async () => {
    const fetchMock = mockFetch();
    await openEdit();
    fireEvent.change(screen.getByLabelText("Working hours"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const calls = fetchMock.mock.calls as Array<[unknown, RequestInit | undefined]>;
      const patch = calls.find(([, init]) => init?.method === "PATCH");
      const body = JSON.parse((patch as unknown as [unknown, RequestInit])[1].body as string);
      expect(body).toEqual({ working_hours_raw: null });
    });
  });

  it("hands the saved row back so the list and panel agree", async () => {
    const onEdited = vi.fn();
    render(panel(opportunity(), onEdited));
    fireEvent.click(await screen.findByRole("button", { name: "Edit details" }));
    await screen.findByLabelText("Job title");
    fireEvent.change(screen.getByLabelText("Location"), { target: { value: "North" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(onEdited).toHaveBeenCalledTimes(1));
    expect(onEdited.mock.calls[0][0]).toMatchObject({ location_raw: "North" });
    // The edit mode closes; the read-only view is back.
    expect(await screen.findByText("Received")).toBeTruthy();
  });

  it("does not save when nothing changed — the Save button is disabled", async () => {
    const fetchMock = mockFetch();
    const onEdited = vi.fn();
    render(panel(opportunity(), onEdited));
    fireEvent.click(await screen.findByRole("button", { name: "Edit details" }));
    await screen.findByLabelText("Job title");

    // Nothing changed, so Save is disabled: a save that writes nothing but a
    // read-back is a request nobody asked for.
    const save = screen.getByRole("button", { name: "Save changes" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(save);

    await waitFor(() => {
      const calls = fetchMock.mock.calls;
      expect(calls.some(([, init]) => init?.method === "PATCH")).toBe(false);
    });
    expect(onEdited).not.toHaveBeenCalled();
    // The read-only view is back.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("Received")).toBeTruthy();
  });

  it("shows the server's message when the save is refused", async () => {
    const fetchMock = mockFetch(() => {
      throw new Error("should not be called");
    });
    fetchMock.mockImplementation(async (_url: string | URL | Request, init?: RequestInit) => {
      if (String(_url).includes("/api/members")) {
        return { ok: true, status: 200, json: async () => STAFF };
      }
      if (init?.method === "PATCH") {
        return {
          ok: false,
          status: 403,
          json: async () => ({ detail: "This job order is shared with you, not assigned to you." }),
          text: async () => "{}",
        };
      }
      return { ok: true, status: 200, json: async () => SAVED };
    });
    await openEdit();
    fireEvent.change(screen.getByLabelText("Location"), { target: { value: "North" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "This job order is shared with you, not assigned to you.",
    );
    // The form stays open with the typed value.
    expect((screen.getByLabelText("Location") as HTMLInputElement).value).toBe("North");
  });
});
