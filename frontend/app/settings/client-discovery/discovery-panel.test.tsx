import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DiscoveryPanel } from "./discovery-panel";

/**
 * The discovery panel's three promises, pinned:
 *
 * 1. Nothing is scanned until the recruiter asks — the empty state offers a
 *    button, and pressing it is what POSTs.
 * 2. A finished run renders exactly what the server ranked — domains, top
 *    contact, the enrichment receipt — and a row already acted on cannot be
 *    selected again.
 * 3. Create sends precisely the ticked domains, nothing more.
 *
 * allow-hardcode: the strings below are test fixtures and verbatim copies of
 * user-facing labels being asserted on. Nothing is matched against them at
 * runtime.
 */

type Call = { url: string; init?: RequestInit };

let calls: Call[] = [];
let runBody: unknown = { run: null };

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      if (url.endsWith("/scan")) {
        return new Response(JSON.stringify({ id: "r1", status: "pending" }), {
          status: 202,
        });
      }
      if (url.endsWith("/clients")) {
        return new Response(
          JSON.stringify({
            results: [{ domain: "acme.com.sg", outcome: "created", contacts_added: 2 }],
          }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify(runBody), { status: 200 });
    }),
  );
}

function doneRun(overrides: Record<string, unknown> = {}) {
  return {
    run: {
      id: "r1",
      status: "done",
      lookback_days: 90,
      created_at: "2026-08-02T09:00:00+00:00",
      started_at: "2026-08-02T09:00:01+00:00",
      finished_at: "2026-08-02T09:01:00+00:00",
      inbox_scanned: 120,
      sent_scanned: 40,
      messages_truncated: false,
      domains_truncated: false,
      clients_enriched: 3,
      contacts_added: 7,
      error: null,
      results: [
        {
          domain: "acme.com.sg",
          score: 25.5,
          received: 9,
          sent: 3,
          unique_contacts: 2,
          last_activity: "2026-08-01T08:00:00+00:00",
          created: false,
          contacts: [
            {
              email: "jane@acme.com.sg",
              name: "Jane Lim",
              inbound: 9,
              outbound: 3,
              last_activity: "2026-08-01T08:00:00+00:00",
            },
          ],
        },
        {
          domain: "globex.com",
          score: 6,
          received: 1,
          sent: 0,
          unique_contacts: 1,
          last_activity: "2026-07-01T08:00:00+00:00",
          created: true,
          contacts: [],
        },
      ],
      ...overrides,
    },
  };
}

beforeEach(() => {
  calls = [];
  runBody = { run: null };
  stubFetch();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DiscoveryPanel", () => {
  it("offers a scan and POSTs only when asked", async () => {
    render(<DiscoveryPanel />);

    const button = await screen.findByRole("button", { name: "Scan my mailbox" });
    // Mounting fetched the run and nothing else — no POST without a click.
    expect(calls.every((c) => (c.init?.method ?? "GET") === "GET")).toBe(true);

    fireEvent.click(button);
    await waitFor(() =>
      expect(
        calls.some(
          (c) => c.url.endsWith("/scan") && c.init?.method === "POST",
        ),
      ).toBe(true),
    );
  });

  it("renders a finished run: ranking, receipt, and spent rows locked", async () => {
    runBody = doneRun();
    render(<DiscoveryPanel />);

    expect(await screen.findByText("acme.com.sg")).toBeDefined();
    expect(screen.getByText(/Jane Lim/)).toBeDefined();
    // The enrichment receipt — the automatic backfill is reported, not silent.
    expect(
      screen.getByText(/Filled in 7 contacts across 3 existing clients/),
    ).toBeDefined();

    const spent = screen.getByRole("checkbox", {
      name: "Add globex.com as a client",
    }) as HTMLInputElement;
    expect(spent.disabled).toBe(true);
    expect(spent.checked).toBe(true);
    expect(screen.getByText("Added")).toBeDefined();
  });

  it("creates exactly the ticked domains", async () => {
    runBody = doneRun();
    render(<DiscoveryPanel />);

    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Add acme.com.sg as a client" }),
    );
    fireEvent.click(screen.getByRole("button", { name: /Add 1 selected as client/ }));

    await waitFor(() => {
      const create = calls.find((c) => c.url.endsWith("/clients"));
      expect(create).toBeDefined();
      expect(JSON.parse(String(create!.init!.body))).toEqual({
        domains: ["acme.com.sg"],
      });
    });
    expect(await screen.findByText(/Added 1 client and 2 contacts\./)).toBeDefined();
  });

  it("shows a failed run's own words and offers a retry", async () => {
    runBody = {
      run: {
        ...doneRun().run,
        status: "failed",
        results: null,
        error: "Microsoft would not let us read this mailbox.",
      },
    };
    render(<DiscoveryPanel />);

    expect(
      await screen.findByText("Microsoft would not let us read this mailbox."),
    ).toBeDefined();
    expect(screen.getByRole("button", { name: "Scan again" })).toBeDefined();
  });
});
