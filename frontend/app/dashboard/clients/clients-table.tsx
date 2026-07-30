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
 * Status is a badge rather than plain text because two of the five mean
 * opposite things and were being read as the same word-shaped grey. Suspended
 * is amber — a live client on hold, something you are expected to come back
 * to; archived stays the muted grey of a row that is finished with. The
 * colouring is `data-status` in `app.css`, so the labels above stay the only
 * place the wording lives.
 *
 * allow-hardcode: the strings here are user-facing copy, not a list anything
 * is matched against.
 */

const STATUS_LABEL: Record<Client["status"], string> = {
  unconfirmed: "Unconfirmed",
  confirmed: "Confirmed",
  suspended: "Suspended",
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
      <table className="jo-table jo-table-clients">
        {/* Four columns, so this table fits the master column outright and
            does not need the sideways scroll the job-orders table lives with.
            It only did before because the floor was shared: forced to the
            eight-column table's width, Status and Last seen sat off the right
            edge of a card that was already as wide as the screen allowed.

            Status is 23% because the badge sets it, and the badge is wider
            than its label looks: uppercased and letter-spaced, "UNCONFIRMED"
            measures 107px, so with the cell's 28px of padding it needs 135px
            and 23% of the 600px floor gives 138. Last seen is 20% for the
            same kind of reason — the date does not wrap. Name and Mail domain
            do wrap, so they are the two that can give the room. */}
        <colgroup>
          <col style={{ width: "30%" }} />
          <col style={{ width: "27%" }} />
          <col style={{ width: "23%" }} />
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
                <td className="jo-td">
                  <span className="cl-status" data-status={row.status}>
                    {STATUS_LABEL[row.status]}
                  </span>
                </td>
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
