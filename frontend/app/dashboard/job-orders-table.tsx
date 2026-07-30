"use client";

import { ProtectedBadge, flagged } from "./codes";
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

/** Every width is measured against the table's 1040px floor, which is the
 *  narrowest it is ever drawn — under that, `.jo-table-card` scrolls.
 *
 *  Each column is sized against the widest thing it actually holds, because
 *  `table-layout: fixed` means a column cannot ask for more later:
 *
 *  - Quality, 17%: the "Needs review 7/9" pill is 138px and the cell adds 28px
 *    of padding, so it needs 166px and gets 177. At the 10% it had, it got 96
 *    and the pill folded into a four-line block — the column read as chopped
 *    off, and it set the height of every row carrying that state.
 *  - Location, 12%: "Not mentioned" is 96px of italic, so the cell needs 124px
 *    and gets 125. It is the commonest value in the column; at 9% it wrapped
 *    to three lines.
 *  - Hours and Duration are the raw paragraphs, clamped to two lines (see
 *    `.jo-clamp`). They need enough width for two lines to say something: at
 *    10% they held about seven characters a line, which is how "Not mentioned"
 *    came out as "Not mention / ed" — too narrow to break between words, so it
 *    broke inside one.
 *
 *  The floor moved from 960 rather than taking Quality's room from the others,
 *  which had none to give. It costs a little more sideways scrolling inside
 *  the card, which is already how this table meets a narrow screen. */
const COLUMNS: { key: SortKey; label: string; width: string }[] = [
  { key: "received", label: "Received", width: "10%" },
  { key: "company", label: "Company", width: "14%" },
  { key: "position", label: "Position", width: "14%" },
  { key: "salary", label: "Salary", width: "10%" },
  { key: "hours", label: "Hours", width: "13%" },
  { key: "duration", label: "Duration", width: "10%" },
  { key: "location", label: "Location", width: "12%" },
  { key: "quality", label: "Quality", width: "17%" },
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
      <table className="jo-table jo-table-jobs">
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
                <Td clamp>{row.working_hours_raw}</Td>
                <Td clamp>{row.duration_raw}</Td>
                <Td>{row.location_raw}</Td>
                <td className="jo-td">
                  <QualityBadge row={row} />
                  {/* No column of its own — there is no width left for one at
                      this table's fixed layout, and a mark that is only on
                      some rows would leave an empty column on most. It shares
                      the quality cell because it is the same kind of thing: a
                      reason to open the row. The decoded codes themselves are
                      in the panel, where there is room to say whose words
                      they are. */}
                  {flagged(row) && <ProtectedBadge />}
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

/** `clamp` holds a cell to two lines. Only for the fields that are prose an
 *  email wrote rather than a value it stated: one shift schedule ran to forty
 *  words, and a table row is as tall as its tallest cell, so a single row was
 *  five times the height of its neighbours.
 *
 *  Nothing is lost. The row opens the panel, which shows the field whole —
 *  the same reason Requirements and Description stopped being columns. `title`
 *  is a convenience on top of that, not the way to read it: a tooltip is
 *  unreachable on touch and by keyboard. */
function Td({
  children,
  nowrap = false,
  clamp = false,
}: {
  children: string | null;
  nowrap?: boolean;
  clamp?: boolean;
}) {
  return (
    <td className="jo-td" data-nowrap={nowrap ? "yes" : undefined}>
      {clamp ? (
        <div className="jo-clamp" title={children ?? undefined}>
          <Value text={children} />
        </div>
      ) : (
        <Value text={children} />
      )}
    </td>
  );
}
