"use client";

import { Breakable } from "../breakable";
import type { Opportunity } from "./opportunities";

/**
 * The shared vocabulary for showing an extracted value.
 *
 * It lives in one file because the rule it enforces is one rule: a value the
 * email did not contain says so, in words, in the muted style (§15). A blank
 * cell or an em dash reads as "this job has no salary", which is a claim about
 * the vacancy rather than about the email — and the AI is forbidden from
 * making it, so the page must not make it either.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

/** A value, or the fact that the email did not contain one. */
export function Value({ text }: { text: string | null }) {
  if (!text) return <span className="muted">Not mentioned</span>;
  return <Breakable text={text} />;
}

/** Absolute, not "3 days ago": these views do not re-render on a timer, so a
 *  relative date would quietly age into a lie while someone reads it. */
export function day(iso: string | null): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** "30 Jul" — the compact form for a list column that is scanned by recency.
 *  The year stays in `day()`, used by the detail panel and as a `title` tooltip
 *  on the cell; in a list of recent rows the day and month are what the eye
 *  sorts on, and the column is also the widest fixed-layout table's tightest
 *  column, so dropping five characters buys room for the column after it. */
export function dayShort(iso: string | null): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
  });
}

export function when(iso: string | null): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function number(value: number): string {
  // No forced decimals: "6000.00" for a salary someone wrote as "6k" adds a
  // precision the email never carried.
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/**
 * The normalised range, where one could be parsed at all.
 *
 * `== null`, not falsy: a genuine 0 is an extracted value, and treating it as
 * absent would silently hide an unpaid or commission-only posting.
 */
export function salaryRange(row: Opportunity): string | null {
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

/**
 * The clean range with the sender's own words underneath.
 *
 * Both, not one: "SGD 5,000–6,000 per month" is what a recruiter compares
 * across rows, and "5-6k neg." is what they will recognise from the email and
 * can defend to a client. Showing only our parse would present it as a
 * quotation.
 *
 * Each line is its own block (`jo-salary-line`): without that, the raw text ran
 * on from the range as "2,500 per month$2500" — two inline runs with nothing to
 * separate them. The block forces the raw onto its own line in both the table
 * cell (which clamps to two lines) and the panel.
 */
export function Salary({ row }: { row: Opportunity }) {
  const range = salaryRange(row);
  if (!range) return <Value text={row.salary_raw} />;
  return (
    <>
      <span className="jo-salary-line">
        <Breakable text={range} />
      </span>
      {row.salary_raw && (
        <span className="jo-salary-line muted jo-sub">
          <Breakable text={row.salary_raw} />
        </span>
      )}
    </>
  );
}

/** "1 email" / "2 emails" — a plural s on a count of one reads as a bug. */
export function plural(n: number, noun: string): string {
  return `${n.toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;
}
