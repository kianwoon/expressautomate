"use client";

import { day } from "../format";
import type { Client, ClientSort, ClientSortKey } from "../clients";

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
 * The sortable headers follow `job-orders-table.tsx`: click to sort ascending,
 * click again to reverse. The sort is server-side, so the order a recruiter
 * sees is the order paging steps through — not a re-sort of the current page
 * that would disagree with the next one.
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

/** The server sort key each column maps to. Order matches the `<colgroup>` so
 *  a reader scanning down sees the same columns in both. Phone sits beside
 *  Name because "who is this and what do I dial" is the pair a recruiter
 *  scans the list for; the buddy who referred the account is a fact about
 *  the row, not the identity of it. */
const COLUMNS: { key: ClientSortKey; label: string }[] = [
  { key: "name", label: "Name" },
  { key: "phone", label: "Phone" },
  { key: "email_domain", label: "Referred by" },
  { key: "status", label: "Status" },
  { key: "last_seen", label: "Last seen" },
];

export function ClientsTable({
  rows,
  selectedId,
  onSelect,
  sort,
  onSort,
}: {
  rows: Client[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  sort: ClientSort;
  onSort: (sort: ClientSort) => void;
}) {
  return (
    <div className="card jo-table-card">
      <table className="jo-table jo-table-clients">
        {/* Five columns at a 720px floor (`.jo-table-clients` in `app.css`),
            below which the card scrolls sideways — the same deal the
            job-orders table lives with, and the cost of adding Phone to a
            table that used to fit the old side-by-side layout outright.

            Each width is measured against that floor, because
            `table-layout: fixed` means a column cannot ask for more later:

            - Name, 26%: the identity column gets the most room; long company
              names wrap.
            - Phone, 19%: a phone number must not wrap (broken across lines it
              stops reading as a number), so the cell holds one line. "+65 6221
              3344" is ~100px of text, and with the cell's 28px of padding it
              needs ~130px — 19% of 720 gives 137.
            - Referred by, 17%: the buddy's name wraps, so it can give room.
            - Status, 21%: the badge sets it — uppercased and letter-spaced,
              "UNCONFIRMED" measures 107px, so with padding it needs 135px and
              21% of 720 gives 151.
            - Last seen, 17%: the date does not wrap, and 17% of 720 gives 122
              for the date's ~110px.
        */}
        <colgroup>
          <col style={{ width: "26%" }} />
          <col style={{ width: "19%" }} />
          <col style={{ width: "17%" }} />
          <col style={{ width: "21%" }} />
          <col style={{ width: "17%" }} />
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
                    aria-label={`Show details for ${row.name}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      onSelect(row.id);
                    }}
                  >
                    {row.name}
                  </button>
                </td>
                <td className="jo-td" data-nowrap="yes">
                  {row.phone ?? <span className="muted">—</span>}
                </td>
                <td className="jo-td">
                  {row.buddy_name ?? <span className="muted">—</span>}
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

function Th({
  column,
  sort,
  onSort,
}: {
  column: { key: ClientSortKey; label: string };
  sort: ClientSort;
  onSort: (sort: ClientSort) => void;
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
        // starts ascending — except Last seen, where descending is what anyone
        // clicking a date column means by "sort by date". The same rule
        // `job-orders-table.tsx` applies to its Received column.
        onClick={() =>
          onSort(
            active
              ? { key: column.key, descending: !sort.descending }
              : { key: column.key, descending: column.key === "last_seen" },
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
