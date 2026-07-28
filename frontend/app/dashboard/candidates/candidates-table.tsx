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
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

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
}: {
  rows: Candidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="card jo-table-card">
      <table className="jo-table">
        <colgroup>
          <col style={{ width: "24%" }} />
          <col style={{ width: "20%" }} />
          <col style={{ width: "20%" }} />
          <col style={{ width: "18%" }} />
          <col style={{ width: "18%" }} />
        </colgroup>
        <thead>
          <tr>
            <th className="row-k jo-th">Name</th>
            <th className="row-k jo-th">Title</th>
            <th className="row-k jo-th">Employer</th>
            <th className="row-k jo-th">Stage</th>
            <th className="row-k jo-th">Updated</th>
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
