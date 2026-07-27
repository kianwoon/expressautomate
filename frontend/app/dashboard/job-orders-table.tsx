"use client";

import { Salary, Value, day } from "./format";
import type { Opportunity } from "./opportunities";
import { QualityBadge } from "./quality";

/**
 * The list half of the master–detail.
 *
 * Requirements and description are no longer columns. They are paragraphs, and
 * clamping a paragraph into a fixed-width cell was always a compromise made
 * because there was nowhere else to put it; the panel beside the table is that
 * somewhere. Both are still searchable, because a recruiter looking for
 * "forklift licence" is looking in the requirements — the search runs over the
 * data, not over what is on screen.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export type SortKey =
  | "received"
  | "company"
  | "position"
  | "salary"
  | "hours"
  | "duration"
  | "location"
  | "quality";

export type Sort = { key: SortKey; descending: boolean };

/** Newest first, as the unsorted list already is. Clicking never strands the
 *  user somewhere they cannot get back from by clicking Received again. */
export const DEFAULT_SORT: Sort = { key: "received", descending: true };

const COLUMNS: { key: SortKey; label: string; width: string }[] = [
  { key: "received", label: "Received", width: "11%" },
  { key: "company", label: "Company", width: "16%" },
  { key: "position", label: "Position", width: "18%" },
  { key: "salary", label: "Salary", width: "15%" },
  { key: "hours", label: "Hours", width: "10%" },
  { key: "duration", label: "Duration", width: "10%" },
  { key: "location", label: "Location", width: "10%" },
  { key: "quality", label: "Quality", width: "10%" },
];

export function JobOrdersTable({
  rows,
  sort,
  onSort,
  selectedId,
  onSelect,
}: {
  rows: Opportunity[];
  sort: Sort;
  onSort: (sort: Sort) => void;
  selectedId: string | null;
  onSelect: (row: Opportunity) => void;
}) {
  return (
    <div className="card jo-table-card">
      {/* `table-layout: fixed` is load-bearing. Auto layout sizes columns to
          their content, so one long position title would widen its column
          until the table pushed past the card. Fixed plus the widths above
          means a long value wraps instead. */}
      <table className="jo-table">
        <colgroup>
          {COLUMNS.map((column) => (
            <col key={column.key} style={{ width: column.width }} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {COLUMNS.map((column) => (
              <Th key={column.key} column={column} sort={sort} onSort={onSort} />
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const selected = row.id === selectedId;
            return (
              <tr
                key={row.id}
                className="jo-row"
                data-selected={selected ? "yes" : undefined}
                // A convenience for the mouse, layered over a real control
                // rather than replacing one. The button in the company cell is
                // what the keyboard uses and what a screen reader announces;
                // this row handler exists so a click anywhere in a wide row
                // does the same thing, and it is not the only way in.
                onClick={() => onSelect(row)}
              >
                <Td nowrap>{day(row.received_datetime)}</Td>
                <td className="jo-td jo-td-strong">
                  <button
                    type="button"
                    className="jo-rowbtn"
                    aria-pressed={selected}
                    // The row's own name is not enough on its own: "Acme" read
                    // out of context does not say what pressing it does.
                    aria-label={`Show details for ${row.company_name_raw ?? "this job order"}`}
                    onClick={(event) => {
                      // The row handler would otherwise fire straight after
                      // this one and select the same row twice.
                      event.stopPropagation();
                      onSelect(row);
                    }}
                  >
                    <Value text={row.company_name_raw} />
                  </button>
                </td>
                <Td>{row.job_title_raw}</Td>
                <td className="jo-td">
                  <Salary row={row} />
                </td>
                <Td>{row.working_hours_raw}</Td>
                <Td>{row.duration_raw}</Td>
                <Td>{row.location_raw}</Td>
                <td className="jo-td">
                  <QualityBadge row={row} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
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
      className="row-k jo-th"
      // The arrow is invisible to a screen reader, and a table that has
      // silently reordered itself is indistinguishable from one that lost rows.
      aria-sort={active ? (sort.descending ? "descending" : "ascending") : "none"}
      data-active={active ? "yes" : undefined}
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

function Td({ children, nowrap = false }: { children: string | null; nowrap?: boolean }) {
  return (
    <td className="jo-td" data-nowrap={nowrap ? "yes" : undefined}>
      <Value text={children} />
    </td>
  );
}
