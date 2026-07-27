import { salaryRange } from "./format";
import type { Opportunity, QualityState } from "./opportunities";
import type { Sort, SortKey } from "./job-orders-table";

/**
 * Search and ordering, over the page that has been fetched.
 *
 * Both are client-side and both are honest about their scope: the endpoint
 * pages server-side, so this sees fifty rows rather than the whole set, and the
 * table says so beside the count. Doing it here keeps the search instant and
 * costs a round trip per keystroke that would find nothing the browser is not
 * already holding.
 */

/**
 * Everything the sender wrote on this row, lowercased, for the search.
 *
 * Built from the data and never from what is on screen. "Not mentioned" is our
 * word for an absence, not the sender's: match against it and a search for
 * "mentioned" returns every row with an empty cell, which is the opposite of
 * what was asked. Nulls are dropped rather than stringified for the same
 * reason — "null" is a token no email contains.
 *
 * Requirements and description are in here although they are no longer
 * columns. They are where half the searchable substance of a job order lives,
 * and a search that skipped them because the table stopped showing them would
 * be a quiet regression.
 */
export function haystack(row: Opportunity): string {
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
    // The bare digits too. `salaryRange` formats through `toLocaleString`, so
    // the rendered text is "263,000" here and "263.000" on a German browser —
    // searching for either would miss on the other, and nobody types the
    // separator they were not shown. These are unformatted, so "263000"
    // matches everywhere.
    row.salary_min == null ? null : String(row.salary_min),
    row.salary_max == null ? null : String(row.salary_max),
  ]
    .filter((value): value is string => value != null)
    .join("   ")
    .toLowerCase();
}

// How many of each period make a month. Extraction stores the period beside
// the figure precisely so this is possible.
const PER_MONTH: Record<string, number> = {
  hour: 1 / (40 * 4.35),
  day: 1 / 21.75,
  week: 1 / 4.35,
  month: 1,
  year: 12,
};

/**
 * A salary on one scale, so the column sorts by what a job actually pays.
 *
 * Comparing the raw figures put "SGD 30,000 per year" above "SGD 5,000 per
 * month", which is 60,000 a year — the higher-paying role ranked lower, in a
 * product whose users sort by salary to find the better job.
 *
 * An unrecognised or missing period returns null rather than assuming monthly:
 * a wrong position in the ordering is indistinguishable from a right one,
 * whereas an unsortable row sinks to the bottom where its absence is visible.
 * Currency is still not normalised — that needs live rates, and inventing one
 * is the kind of guess §15 exists to forbid.
 */
function monthly(amount: number | null, period: string | null): number | null {
  if (amount == null) return null;
  const factor = period == null ? null : PER_MONTH[period.trim().toLowerCase()];
  return factor == null ? null : amount / factor;
}

/** Worst first when ascending: the rows that need a human are the reason
 *  anyone sorts this column at all. */
const QUALITY_RANK: Record<QualityState, number> = {
  needs_review: 0,
  likely: 1,
  verified: 2,
};

/**
 * The value a column sorts on — never the string the cell renders.
 *
 * Received returns epoch milliseconds, not "Jul 24, 2026": sorted as text that
 * puts April before January, and 2025 in among 2026 wherever the month name
 * happens to fall alphabetically.
 */
function sortValue(row: Opportunity, key: SortKey): string | number | null {
  switch (key) {
    case "received":
      return row.received_datetime ? Date.parse(row.received_datetime) : null;
    case "salary":
      // `??`, not `||`: a genuine 0 is an extracted value, and treating it as
      // absent would sink an unpaid posting to the bottom as though the email
      // had said nothing about pay at all.
      return monthly(row.salary_min ?? row.salary_max ?? null, row.salary_period);
    case "quality":
      return QUALITY_RANK[row.quality_state] ?? 0;
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
  }
}

/** Case-insensitive: "acme" and "Acme" are the same company, and an
 *  uppercase-first ordering files them pages apart. */
function lower(value: string | null): string | null {
  return value == null ? null : value.toLowerCase();
}

export function compare(a: Opportunity, b: Opportunity, sort: Sort): number {
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
