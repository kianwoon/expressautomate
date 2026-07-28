"use client";

import { day } from "../format";
import type { Client } from "../clients";

/**
 * The list half of the clients master-detail.
 *
 * A client-specific sibling of `candidates-table.tsx`, not a generalisation
 * of it: there is no pipeline stage here, only a review status, and a client
 * has no title or employer — it has a name and a mail domain.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

const STATUS_LABEL: Record<Client["status"], string> = {
  unconfirmed: "Unconfirmed",
  confirmed: "Confirmed",
  archived: "Archived",
  merged: "Merged",
};

export function ClientsTable({
  rows,
  selectedId,
  onSelect,
}: {
  rows: Client[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="card jo-table-card">
      <table className="jo-table">
        <colgroup>
          <col style={{ width: "34%" }} />
          <col style={{ width: "28%" }} />
          <col style={{ width: "18%" }} />
          <col style={{ width: "20%" }} />
        </colgroup>
        <thead>
          <tr>
            <th className="row-k jo-th">Name</th>
            <th className="row-k jo-th">Mail domain</th>
            <th className="row-k jo-th">Status</th>
            <th className="row-k jo-th">Last seen</th>
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
                    aria-label={`Show details for ${row.name}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelect(row.id);
                    }}
                  >
                    {row.name}
                  </button>
                </td>
                <td className="jo-td">
                  {row.email_domain ?? <span className="muted">Not mentioned</span>}
                </td>
                <td className="jo-td">{STATUS_LABEL[row.status]}</td>
                <td className="jo-td" data-nowrap="yes">
                  {day(row.last_seen_at) ?? <span className="muted">Never</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
