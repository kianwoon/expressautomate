"use client";

import { Value, day } from "../format";
import type { Candidate } from "../candidates";

/**
 * The list half of the candidates master–detail.
 *
 * A candidate-specific sibling of `job-orders-table.tsx`, not a
 * generalisation of it: the columns and the type are different, and the two
 * screens have nothing to share but layout idioms, which live in the shared
 * `jo-*` CSS classes both already reuse.
 *
 * Column sorting mirrors job-orders: a `Sort` type, a `Th` header with
 * `aria-sort` + a click handler, and the page threading `sort`/`setSort`
 * through. The five columns are the same five the table has always drawn.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

/** The five list columns a recruiter may sort by. The keys are the values the
 *  backend's `CandidateSortKey` accepts — they travel in the query string. */
export type CandidateSortKey = "name" | "title" | "employer" | "stage" | "updated";

/** A sort the table is currently applying. `{ key: "updated", descending: true }`
 *  is the default — the fixed order the list had before sorting landed. */
export type CandidateSort = { key: CandidateSortKey; descending: boolean };

const COLUMNS: { key: CandidateSortKey; label: string }[] = [
  { key: "name", label: "Name" },
  { key: "title", label: "Title" },
  { key: "employer", label: "Employer" },
  { key: "stage", label: "Stage" },
  { key: "updated", label: "Updated" },
];

const STAGE_LABEL: Record<Candidate["pipeline_stage"], string> = {
  new: "New",
  contacted: "Contacted",
  submitted: "Submitted",
  placed: "Placed",
  rejected: "Rejected",
};

export function CandidatesTable({
  rows,
  selectedId,
  onSelect,
  sort,
  onSort,
}: {
  rows: Candidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  sort: CandidateSort;
  onSort: (sort: CandidateSort) => void;
}) {
  return (
    <div className="card jo-table-card">
      <table className="jo-table jo-table-candidates">
        {/* Five columns, and they fit the master column — so this table does
            not scroll sideways, and Stage and Updated are on screen rather
            than off the right edge, which is where the shared floor used to
            put them.

            Updated is 18% because the date does not wrap: it needs 116px and
            gets 119 at the floor. Stage is 16% for the same reason at a
            smaller size — "Contacted" is the longest of the five. Name, Title
            and Employer wrap, so the room comes from them. */}
        <colgroup>
          <col style={{ width: "25%" }} />
          <col style={{ width: "21%" }} />
          <col style={{ width: "20%" }} />
          <col style={{ width: "16%" }} />
          <col style={{ width: "18%" }} />
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
                onClick={() => onSelect(row.id)}
              >
                <td className="jo-td jo-td-strong">
                  <button
                    type="button"
                    className="jo-rowbtn"
                    aria-pressed={selected}
                    aria-label={`Show details for ${row.full_name}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelect(row.id);
                    }}
                  >
                    {row.full_name}
                  </button>
                </td>
                <td className="jo-td">
                  <Value text={row.current_title} />
                </td>
                <td className="jo-td">
                  <Value text={row.current_employer} />
                </td>
                <td className="jo-td">
                  {row.record_status === "merged" ? (
                    <span className="muted">Merged</span>
                  ) : (
                    STAGE_LABEL[row.pipeline_stage]
                  )}
                </td>
                <td className="jo-td" data-nowrap="yes">
                  {day(row.updated_at)}
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
  column: { key: CandidateSortKey; label: string };
  sort: CandidateSort;
  onSort: (sort: CandidateSort) => void;
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
        // starts ascending — except Updated, where descending is what anyone
        // clicking a date column means by "sort by date".
        onClick={() =>
          onSort(
            active
              ? { key: column.key, descending: !sort.descending }
              : { key: column.key, descending: column.key === "updated" },
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
