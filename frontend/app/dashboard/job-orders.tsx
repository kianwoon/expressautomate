"use client";

import { useEffect, useMemo, useState } from "react";

import { OPPORTUNITIES_PATH } from "../api";
import { Breakable } from "../breakable";

/**
 * The job orders, as the spreadsheet this product replaces laid them out —
 * company, position, salary, hours, requirements, description, duration,
 * location — plus the column that spreadsheet never had: when the email
 * arrived.
 *
 * Received date leads and sorts the list by default. In a sheet with no date,
 * a vacancy mailed six weeks ago and one mailed this morning look identical,
 * and the recruiter working down the list has no way to tell which is still
 * open.
 *
 * Nothing is invented on this side either. A null from the API renders as the
 * muted "Not mentioned" — the same vocabulary the account cards use, and for
 * the same reason: the AI is forbidden from filling a gap (§15), so the table
 * must not fill it either. An em dash or a blank cell would read as "no
 * salary", which is a claim about the job rather than about the email.
 *
 * Search and sort are both client-side, and deliberately: the endpoint returns
 * the whole list in one response, bounded by OPPORTUNITIES_PAGE_LIMIT, so
 * there is no second page for a server-side query to reach. A round trip here
 * would add latency to every keystroke and find nothing the browser is not
 * already holding.
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
  job_description: string | null;
  duration_raw: string | null;
  location_raw: string | null;
  quality_state: string;
  review_status: string;
};

type State =
  | { status: "loading" }
  | { status: "ready"; rows: Opportunity[] }
  | { status: "unreadable"; message: string };

type SortKey =
  | "received"
  | "company"
  | "position"
  | "salary"
  | "hours"
  | "duration"
  | "location"
  | "requirements"
  | "description";

type Sort = { key: SortKey; descending: boolean };

/** Newest first, as the unsorted list already was. Clicking never strands the
 *  user somewhere they cannot get back from by clicking Received again. */
const DEFAULT_SORT: Sort = { key: "received", descending: true };

/** A stable identity for "no rows yet", so the memo below is not recomputing
 *  against a fresh array on every render while the fetch is still out. */
const EMPTY: Opportunity[] = [];

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "received", label: "Received" },
  { key: "company", label: "Company" },
  { key: "position", label: "Position" },
  { key: "salary", label: "Salary" },
  { key: "hours", label: "Hours" },
  { key: "duration", label: "Duration" },
  { key: "location", label: "Location" },
  { key: "requirements", label: "Requirements" },
  { key: "description", label: "Description" },
];

export function JobOrders() {
  const [state, setState] = useState<State>({ status: "loading" });
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>(DEFAULT_SORT);

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

  const all = state.status === "ready" ? state.rows : EMPTY;

  // Filter first, then sort, and never in place. `.sort` mutates its receiver,
  // so sorting `state.rows` directly would reorder the array React holds as
  // state — the next render compares that array to itself, sees no change, and
  // paints the old order.
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matched = needle ? all.filter((row) => haystack(row).includes(needle)) : all;
    return [...matched].sort((a, b) => compare(a, b, sort));
  }, [all, query, sort]);

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

  const isFiltered = visible.length !== all.length;

  return (
    <div style={{ marginTop: 48 }}>
      <span className="eyebrow">Job orders</span>
      <h2 style={{ marginTop: 12 }}>Every vacancy we have read.</h2>
      <p className="body" style={{ marginTop: 12, maxWidth: "68ch" }}>
        Each row is what one email actually said — where it said nothing, the cell says so rather
        than guessing. Search any column, or click a heading to sort by it.
      </p>

      <div
        style={{
          marginTop: 20,
          display: "flex",
          alignItems: "center",
          gap: 16,
          flexWrap: "wrap",
        }}
      >
        <input
          className="jo-search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search company, role, salary, location…"
          aria-label="Search job orders"
          style={{ flex: "1 1 18rem" }}
        />
        {/* The count is what stops a filtered table being read as the whole
            list. Without it, a search someone left in the box three minutes
            ago says "we have only seven job orders". */}
        <span className="body muted" style={{ fontSize: "0.875rem" }} aria-live="polite">
          {isFiltered
            ? `${visible.length} of ${all.length} job orders`
            : `${all.length} job order${all.length === 1 ? "" : "s"}`}
        </span>
      </div>

      {/* A header row over nothing looks like a bug in us rather than a search
          that missed, and it offers no way back out. The escape hatch ships
          with the message. */}
      {visible.length === 0 ? (
        <div className="card" style={{ marginTop: 16 }}>
          <p className="body" style={{ margin: 0, maxWidth: "62ch" }}>
            No job order mentions <strong>{query.trim()}</strong>. Only what the emails themselves
            said is searchable — a detail the sender left out is not in here to be found.
          </p>
          <button
            className="btn btn-secondary"
            style={{ marginTop: 16 }}
            onClick={() => setQuery("")}
          >
            Clear the search
          </button>
        </div>
      ) : (
        <>
          {/* The card supplies the border and background; the padding is
              dropped so the header strip can run edge to edge. `overflow-x:
              auto` is the backstop for a phone: the layout is sized to fit
              from about 900px up, and below that scrolling beats nine columns
              crushed to one word each. The note under the table says the
              scroll is there, because a cut-off ninth column with no hint
              reads as a missing feature. */}
          <div className="card" style={{ marginTop: 16, padding: 0, overflowX: "auto" }}>
            {/* `table-layout: fixed` is load-bearing. Auto layout sizes columns
                to their content, so one long description paragraph would widen
                that column until the table pushed past the card — the same
                overflow the `.row` grid solves with minmax(0, 1fr). Fixed plus
                the widths below means a long value wraps instead. */}
            <table
              style={{
                width: "100%",
                minWidth: 900,
                borderCollapse: "collapse",
                tableLayout: "fixed",
                fontSize: "0.875rem",
              }}
            >
              <colgroup>
                <col style={{ width: "9%" }} />
                <col style={{ width: "12%" }} />
                <col style={{ width: "13%" }} />
                <col style={{ width: "12%" }} />
                <col style={{ width: "8%" }} />
                <col style={{ width: "8%" }} />
                <col style={{ width: "10%" }} />
                <col style={{ width: "14%" }} />
                <col style={{ width: "14%" }} />
              </colgroup>
              <thead>
                <tr>
                  {COLUMNS.map((column) => (
                    <Th key={column.key} column={column} sort={sort} onSort={setSort} />
                  ))}
                </tr>
              </thead>
              <tbody>
                {visible.map((row) => (
                  <tr key={row.id} style={{ borderTop: "1px solid var(--line)" }}>
                    {/* The date is the only cell that must not wrap: broken
                        over two lines it stops reading as a date at a glance,
                        and it is what the eye scans this table by. */}
                    <Td nowrap>{day(row.received_datetime)}</Td>
                    <Td strong>{row.company_name_raw}</Td>
                    <Td>{row.job_title_raw}</Td>
                    <Td>
                      <Salary row={row} />
                    </Td>
                    <Td>{row.working_hours_raw}</Td>
                    <Td>{row.duration_raw}</Td>
                    <Td>{row.location_raw}</Td>
                    <Td clamp>{row.requirements}</Td>
                    <Td clamp>{row.job_description}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="body muted" style={{ marginTop: 14, fontSize: "0.8125rem" }}>
            Every value here was written by the sender, not by us. Long requirements and
            descriptions are shortened to keep the rows scannable — hover a cell to read it in
            full, and on a narrow screen scroll the table sideways to reach every column.
          </p>
        </>
      )}
    </div>
  );
}

/**
 * Everything the sender wrote on this row, lowercased, for the search to run
 * against.
 *
 * Built from the data and never from what is on screen. "Not mentioned" is our
 * word for an absence, not the sender's: match against it and a search for
 * "mentioned" returns every row with an empty cell, which is the opposite of
 * what was asked. Nulls are dropped rather than stringified for the same
 * reason — "null" is a token no email contains.
 *
 * The normalised salary range is in here because it is on screen and someone
 * will search the figure they can see; it is derived from salary_min/max, not
 * a phrase we invented.
 */
function haystack(row: Opportunity): string {
  return [
    row.company_name_raw,
    row.job_title_raw,
    row.salary_raw,
    salaryRange(row),
    row.working_hours_raw,
    row.duration_raw,
    row.location_raw,
    row.requirements,
    row.job_description,
  ]
    .filter((value): value is string => value != null)
    .join("   ")
    .toLowerCase();
}

/**
 * The value a column sorts on — never the string the cell renders.
 *
 * Received returns epoch milliseconds, not "Jul 24, 2026": sorted as text that
 * puts April before January, and 2025 in among 2026 wherever the month name
 * happens to fall alphabetically.
 *
 * Salary returns the parsed number, not the sender's words: as text "SGD
 * 263,000" sorts below "$3,500" because "$" precedes "S", so a recruiter
 * ordering by pay gets an ordering by punctuation.
 */
function sortValue(row: Opportunity, key: SortKey): string | number | null {
  switch (key) {
    case "received":
      return row.received_datetime ? Date.parse(row.received_datetime) : null;
    case "salary":
      // `??`, not `||`: a genuine 0 is an extracted value, and treating it as
      // absent would sink an unpaid posting to the bottom as though the email
      // had said nothing about pay at all.
      return row.salary_min ?? row.salary_max ?? null;
    case "company":
      return lower(row.company_name_raw);
    case "position":
      return lower(row.job_title_raw);
    case "hours":
      return lower(row.working_hours_raw);
    case "duration":
      return lower(row.duration_raw);
    case "location":
      return lower(row.location_raw);
    case "requirements":
      return lower(row.requirements);
    case "description":
      return lower(row.job_description);
  }
}

/** Case-insensitive: "acme" and "Acme" are the same company, and an
 *  uppercase-first ordering files them pages apart. */
function lower(value: string | null): string | null {
  return value == null ? null : value.toLowerCase();
}

function compare(a: Opportunity, b: Opportunity, sort: Sort): number {
  const left = sortValue(a, sort.key);
  const right = sortValue(b, sort.key);

  // Missing values sink in BOTH directions, which is why this is settled
  // before the direction is applied. A null is not "smallest": let it be one
  // and reversing the sort floats every row the email said nothing about to
  // the top, burying the rows that actually carry the value being sorted on.
  if (left == null && right == null) return 0;
  if (left == null) return 1;
  if (right == null) return -1;

  const order =
    typeof left === "number" && typeof right === "number"
      ? left - right
      : String(left).localeCompare(String(right));
  return sort.descending ? -order : order;
}

function Th({
  column,
  sort,
  onSort,
}: {
  column: { key: SortKey; label: string };
  sort: Sort;
  onSort: (sort: Sort) => void;
}) {
  const active = sort.key === column.key;
  return (
    <th
      className="row-k"
      // The arrow is invisible to a screen reader, and a table that has
      // silently reordered itself is indistinguishable from one that lost
      // rows.
      aria-sort={active ? (sort.descending ? "descending" : "ascending") : "none"}
      style={{
        textAlign: "left",
        padding: "12px 14px",
        background: "var(--surface-alt)",
        borderBottom: "1px solid var(--line)",
        fontWeight: 650,
        color: active ? "var(--ink-700)" : undefined,
      }}
    >
      <button
        type="button"
        className="jo-sort"
        // Re-clicking the sorted column reverses it; moving to a new column
        // starts ascending — except Received, where descending is what anyone
        // clicking a date column means by "sort by date".
        onClick={() =>
          onSort(
            active
              ? { key: column.key, descending: !sort.descending }
              : { key: column.key, descending: column.key === "received" },
          )
        }
      >
        <span>{column.label}</span>
        <span className="jo-arrow" aria-hidden="true">
          {active ? (sort.descending ? "↓" : "↑") : ""}
        </span>
      </button>
    </th>
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

function Td({
  children,
  strong = false,
  nowrap = false,
  clamp = false,
}: {
  children: React.ReactNode;
  strong?: boolean;
  nowrap?: boolean;
  clamp?: boolean;
}) {
  const plain: string | null | undefined =
    typeof children === "string" || children == null ? (children ?? null) : undefined;
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
      {/* Clamping hides the tail of a paragraph, so the tail has to stay
          reachable somewhere, and the title is that somewhere. Only set where
          there is text: a tooltip reading "Not mentioned" just repeats the
          cell under the cursor. */}
      {clamp && plain ? (
        <span className="jo-clamp" title={plain}>
          <Breakable text={plain} />
        </span>
      ) : plain !== undefined ? (
        <Value text={plain} />
      ) : (
        children
      )}
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
