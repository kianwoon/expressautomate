"use client";

import { ProtectedBadge, flagged } from "./codes";
import { Salary, Value, day, dayShort, salaryRange } from "./format";
import type { Opportunity } from "./opportunities";
import { Initials } from "./person";
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

/** Every width is measured against the table's 1000px floor, which is the
 *  narrowest it is ever drawn — under that, `.jo-table-card` scrolls.
 *
 *  Each column is sized against the widest thing it actually holds, because
 *  `table-layout: fixed` means a column cannot ask for more later:
 *
 *  - Received, 8%: the date is shown short ("30 Jul", via `dayShort`) because
 *    this column is also the table's tightest, and the five characters the
 *    year cost were the difference between the floor and the list column on a
 *    13" laptop. The full date with year is the cell's `title` and lives in
 *    the panel; in a list scanned by recency the day and month are what the
 *    eye sorts on.
 *  - Quality, 19%: the "Needs review 7/9" pill is 138px and the cell adds 28px
 *    of padding, so it needs 166px and gets 190. At the 10% it had, it got 96
 *    and the pill folded into a four-line block — the column read as chopped
 *    off, and it set the height of every row carrying that state.
 *  - Location, 12%: "Not mentioned" is 96px of italic, so the cell needs 124px
 *    and gets 120. It is the commonest value in the column; at 9% it wrapped
 *    to three lines.
 *  - Hours and Duration are the raw paragraphs, clamped to two lines (see
 *    `.jo-clamp`). They need enough width for two lines to say something: at
 *    10% they held about seven characters a line, which is how "Not mentioned"
 *    came out as "Not mention / ed" — too narrow to break between words, so it
 *    broke inside one.
 *
 *  The floor moved down from 1040 once Received gave up the year, rather than
 *  taking Quality's room from the others, which had none to give.
 *
 *  1000px is still more than the list column is ever given: `.jo-split` hands
 *  it 1.85 of 2.85 parts, so even a 1440px screen leaves it about 900px and
 *  the last column would sit off the edge behind a scrollbar nobody found.
 *  Rather than crush eight columns, the two prose ones — Hours and Duration —
 *  are dropped below that width by a container query in `job-orders.css`,
 *  which takes the floor to 730px. They are the two already clamped to two
 *  lines here and shown whole in the panel, so nothing becomes unreachable.
 *
 *  The widths live in CSS rather than inline, because the narrow case has to
 *  override them and an inline style cannot be overridden by a stylesheet. */
const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "received", label: "Received" },
  { key: "company", label: "Company" },
  { key: "position", label: "Position" },
  { key: "salary", label: "Salary" },
  { key: "hours", label: "Hours" },
  { key: "duration", label: "Duration" },
  { key: "location", label: "Location" },
  { key: "quality", label: "Quality" },
];

/** The full salary text, plain, for the cell's `title`. Mirrors how `Salary`
 *  composes its two lines — the normalised range, then the sender's words — so
 *  a clamped cell reveals on hover exactly what it folded away. The tooltip is
 *  a convenience; the canonical place for the whole sentence is the panel. */
function salaryTitle(row: Opportunity): string | undefined {
  const range = salaryRange(row);
  const raw = row.salary_raw;
  if (range && raw) return `${range}  ·  ${raw}`;
  return range ?? raw ?? undefined;
}

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
            <col key={column.key} data-col={column.key} />
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
                <Td nowrap title={day(row.received_datetime) ?? undefined}>
                  {dayShort(row.received_datetime)}
                </Td>
                <td className="jo-td jo-td-strong jo-company-cell">
                  {/* Whose job order it is: the buddy who referred the client
                      owns the account. Falls back to the internal assignee
                      only when no buddy is linked. Outside the button on
                      purpose — the owner is a fact about the row, not part of
                      the "show details" action. Larger than it was: at 18px the
                      disc read as a coloured speck beside the name, and the
                      one piece of per-row identity in the table vanished at a
                      glance. 26px is legible against the cell without crowding
                      the company name onto a second line. */}
                  <span
                    className="jo-owner"
                    title={row.buddy_name ?? row.assignee_name ?? "Unassigned"}
                  >
                    {row.buddy_name ? (
                      <Initials name={row.buddy_name} seed={row.buddy_name} size={26} />
                    ) : row.assigned_user_id ? (
                      <Initials
                        name={row.assignee_name ?? "?"}
                        seed={row.assigned_user_id}
                        size={26}
                      />
                    ) : (
                      <span className="jo-owner-empty" role="img" aria-label="Unassigned" />
                    )}
                  </span>
                  <button
                    type="button"
                    className="jo-rowbtn"
                    aria-pressed={selected}
                    // The row's own name is not enough on its own: "Acme" read
                    // out of context does not say what pressing it does.
                    aria-label={`Show details for ${row.company_name_raw ?? "this job order"}`}
                    // The name is clamped to one line in the cell (see
                    // `.jo-company-cell .jo-rowbtn`); this hover is the
                    // convenience that reveals the rest, the same deal every
                    // clamped cell in the table offers.
                    title={row.company_name_raw ?? undefined}
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
                <Td clamp>{row.job_title_raw}</Td>
                <td className="jo-td">
                  {/* Clamped, like the prose columns beside it. Salary carries
                      two lines — the normalised range and the sender's own
                      words — and `salary_raw` is the email's full sentence,
                      which can run the length of a paragraph when a posting
                      banded pay by experience. The whole thing is one hover
                      (`title`) and one panel away; the cell holds it to two
                      lines so one long row does not set the height of every
                      row that shares its state.

                      This cell is also the row's height anchor. `jo-salary-clamp`
                      (see `job-orders.css`) reserves its second line even when
                      the row has no range or no raw text, so a row never drops
                      to a single line because a particular posting said less
                      about pay than its neighbour did. Uniform rows are what
                      make the table scannable; the prose columns beside it
                      still clamp to two lines but are free to use one. */}
                  <div className="jo-clamp jo-salary-clamp" title={salaryTitle(row)}>
                    <Salary row={row} />
                  </div>
                </td>
                <Td clamp>{row.working_hours_raw}</Td>
                <Td clamp>{row.duration_raw}</Td>
                <Td clamp>{row.location_raw}</Td>
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
  title,
}: {
  children: string | null;
  nowrap?: boolean;
  clamp?: boolean;
  title?: string;
}) {
  return (
    <td className="jo-td" data-nowrap={nowrap ? "yes" : undefined} title={title}>
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
