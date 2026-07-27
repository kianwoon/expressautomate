"use client";

import { useEffect, useState } from "react";

import { OPPORTUNITIES_PATH } from "../api";
import { Breakable } from "../breakable";

/**
 * The job orders, as the spreadsheet this product replaces laid them out —
 * company, position, salary, hours, requirements, duration, location — plus
 * the column that spreadsheet never had: when the email arrived.
 *
 * Received date leads and sorts the list. In a sheet with no date, a vacancy
 * mailed six weeks ago and one mailed this morning look identical, and the
 * recruiter working down the list has no way to tell which is still open.
 *
 * Nothing is invented on this side either. A null from the API renders as the
 * muted "Not mentioned" — the same vocabulary the account cards use, and for
 * the same reason: the AI is forbidden from filling a gap (§15), so the table
 * must not fill it either. An em dash or a blank cell would read as "no
 * salary", which is a claim about the job rather than about the email.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
 */

type Opportunity = {
  id: string;
  received_datetime: string | null;
  company_name_raw: string | null;
  job_title_raw: string | null;
  salary_raw: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  working_hours_raw: string | null;
  requirements: string | null;
  duration_raw: string | null;
  location_raw: string | null;
  quality_state: string;
  review_status: string;
};

type State =
  | { status: "loading" }
  | { status: "ready"; rows: Opportunity[] }
  | { status: "unreadable"; message: string };

export function JobOrders() {
  const [state, setState] = useState<State>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await fetch(OPPORTUNITIES_PATH, {
          credentials: "include",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!res.ok) {
          setState({
            status: "unreadable",
            message:
              // A 401 is our session, not the extraction. Saying "we could not
              // read your job orders" for an expired cookie sends the user to
              // look at the wrong thing.
              res.status === 401
                ? "Your session has expired. Sign in again to see your job orders."
                : "We could not load your job orders just now.",
          });
          return;
        }
        const body = (await res.json()) as { opportunities: Opportunity[] };
        setState({ status: "ready", rows: body.opportunities });
      } catch {
        // An aborted fetch is this component unmounting, not a failure. Left
        // in "loading" deliberately: there is nobody to show a message to.
        if (!controller.signal.aborted) {
          setState({ status: "unreadable", message: "We could not reach the server." });
        }
      }
    })();
    return () => controller.abort();
  }, []);

  if (state.status === "loading") {
    return (
      <div style={{ marginTop: 48 }}>
        <span className="eyebrow">Job orders</span>
        <p className="body muted" style={{ marginTop: 12 }}>
          Loading your job orders.
        </p>
      </div>
    );
  }

  if (state.status === "unreadable") {
    return (
      <div style={{ marginTop: 48 }}>
        <span className="eyebrow">Job orders</span>
        <p className="body" style={{ marginTop: 12, maxWidth: "62ch" }}>
          {state.message}
        </p>
      </div>
    );
  }

  // No empty table shell. The "0 job orders found" stat above already says
  // this, and a header row with nothing under it reads as a broken component
  // rather than as an honest zero.
  if (state.rows.length === 0) return null;

  return (
    <div style={{ marginTop: 48 }}>
      <span className="eyebrow">Job orders</span>
      <h2 style={{ marginTop: 12 }}>Every vacancy we have read.</h2>
      <p className="body" style={{ marginTop: 12, maxWidth: "68ch" }}>
        Newest first. Each row is what one email actually said — where it said nothing, the cell
        says so rather than guessing.
      </p>

      {/* The card supplies the border and background; the padding is dropped
          so the header strip can run edge to edge. `overflow-x: auto` is the
          backstop for a phone: the layout is sized to fit from about 700px up,
          and below that scrolling beats eight columns crushed to one word
          each. */}
      <div className="card" style={{ marginTop: 20, padding: 0, overflowX: "auto" }}>
        {/* `table-layout: fixed` is load-bearing. Auto layout sizes columns to
            their content, so one long requirements paragraph would widen that
            column until the table pushed past the card — the same overflow the
            `.row` grid solves with minmax(0, 1fr). Fixed plus the widths below
            means a long value wraps instead. */}
        <table
          style={{
            width: "100%",
            minWidth: 720,
            borderCollapse: "collapse",
            tableLayout: "fixed",
            fontSize: "0.875rem",
          }}
        >
          <colgroup>
            <col style={{ width: "9%" }} />
            <col style={{ width: "14%" }} />
            <col style={{ width: "15%" }} />
            <col style={{ width: "13%" }} />
            <col style={{ width: "10%" }} />
            <col style={{ width: "9%" }} />
            <col style={{ width: "11%" }} />
            <col style={{ width: "19%" }} />
          </colgroup>
          <thead>
            <tr>
              <Th>Received</Th>
              <Th>Company</Th>
              <Th>Position</Th>
              <Th>Salary</Th>
              <Th>Hours</Th>
              <Th>Duration</Th>
              <Th>Location</Th>
              <Th>Requirements</Th>
            </tr>
          </thead>
          <tbody>
            {state.rows.map((row) => (
              <tr key={row.id} style={{ borderTop: "1px solid var(--line)" }}>
                {/* The date is the only cell that must not wrap: broken over
                    two lines it stops reading as a date at a glance, and it is
                    what the eye scans this table by. */}
                <Td nowrap>{day(row.received_datetime)}</Td>
                <Td strong>{row.company_name_raw}</Td>
                <Td>{row.job_title_raw}</Td>
                <Td>
                  <Salary row={row} />
                </Td>
                <Td>{row.working_hours_raw}</Td>
                <Td>{row.duration_raw}</Td>
                <Td>{row.location_raw}</Td>
                <Td>{row.requirements}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="body muted" style={{ marginTop: 14, fontSize: "0.8125rem" }}>
        Every value here was written by the sender, not by us.
      </p>
    </div>
  );
}

/**
 * The clean range where one could be parsed, the sender's own words underneath.
 *
 * Both, not one: "SGD 5,000–6,000 per month" is what a recruiter compares
 * across rows, and "5-6k neg." is what they will recognise from the email and
 * what they can defend to a client. Showing only the normalised figure would
 * present our parse as a quotation.
 */
function Salary({ row }: { row: Opportunity }) {
  const range = salaryRange(row);
  if (!range) return <Value text={row.salary_raw} />;
  return (
    <>
      <Breakable text={range} />
      {row.salary_raw && (
        <span
          className="muted"
          style={{ display: "block", marginTop: 2, fontSize: "0.8125rem" }}
        >
          <Breakable text={row.salary_raw} />
        </span>
      )}
    </>
  );
}

function salaryRange(row: Opportunity): string | null {
  // `== null`, not falsy: a genuine 0 is an extracted value, and treating it
  // as absent would silently hide an unpaid or commission-only posting.
  const min = row.salary_min;
  const max = row.salary_max;
  if (min == null && max == null) return null;
  const amount =
    min != null && max != null && min !== max
      ? `${number(min)}–${number(max)}`
      : number(min ?? (max as number));
  const currency = row.salary_currency ? `${row.salary_currency} ` : "";
  const period = row.salary_period ? ` per ${row.salary_period}` : "";
  return `${currency}${amount}${period}`;
}

function number(value: number): string {
  // No forced decimals: "6000.00" for a salary someone wrote as "6k" adds a
  // precision the email never carried.
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th
      className="row-k"
      style={{
        textAlign: "left",
        padding: "12px 14px",
        background: "var(--surface-alt)",
        borderBottom: "1px solid var(--line)",
        fontWeight: 650,
      }}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  strong = false,
  nowrap = false,
}: {
  children: React.ReactNode;
  strong?: boolean;
  nowrap?: boolean;
}) {
  return (
    <td
      style={{
        padding: "12px 14px",
        verticalAlign: "top",
        // The same backstop `.row` uses: a fixed-layout column cannot widen,
        // so an unbreakable token would otherwise print straight over its
        // neighbour.
        overflowWrap: "anywhere",
        whiteSpace: nowrap ? "nowrap" : undefined,
        fontWeight: strong ? 600 : undefined,
      }}
    >
      {typeof children === "string" || children === null ? <Value text={children} /> : children}
    </td>
  );
}

/** A value, or the fact that the email did not contain one (§15). */
function Value({ text }: { text: string | null }) {
  if (!text) return <span className="muted">Not mentioned</span>;
  return <Breakable text={text} />;
}

/** Absolute, not "3 days ago": this table does not re-render on a timer, so a
 *  relative date would quietly age into a lie while someone reads it. */
function day(iso: string | null): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
