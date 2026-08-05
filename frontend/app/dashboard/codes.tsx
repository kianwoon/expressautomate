"use client";

import { Breakable } from "../breakable";
import { attributeLabel } from "../settings/glossary-data";
import type { DecodedCode, Opportunity } from "./opportunities";

/**
 * The shorthand a client wrote, and what this agency's glossary says it means.
 *
 * Two different kinds of thing sit side by side here and must never blur into
 * one. `C/F` is quotation: those three characters were in the email. "Chinese
 * female" is not — it is the agency's own definition, applied by us. So the
 * code is set in a monospaced quote and the meaning is plainly attributed to
 * the glossary, because a decoded phrase that reads as the sender's words is
 * how an agency ends up defending wording nobody sent.
 *
 * Where a code refers to a protected characteristic the job order is marked.
 * Decoding it and saying nothing would be worse than not decoding it at all:
 * it would turn a client's shorthand into a clean, filterable field with
 * nothing to say a person should look at it. The mark states what the email
 * said and what the glossary says it means, and stops there — it is not an
 * error, not a finding against the agency, and this page gives no advice.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

/** Absent, not empty: the field is optional on the endpoint. */
export function codesOf(row: Opportunity): DecodedCode[] {
  return row.codes ?? [];
}

/** One entry per distinct code+meaning the email used, with how many times.

 *  A bulk recruitment email can contain the same shorthand many times — eight
 *  "JD" in one message is real, not a bug, but eight identical lines carry no
 *  more information than one. Collapsing by what the recruiter actually sees
 *  (the code and the glossary meaning) keeps the list a list of the codes used,
 *  with a `(×N)` count when a code recurred. The per-occurrence offsets are
 *  still in the raw data for anyone who needs them; they are just not the thing
 *  to lay out one row each. */
export type CollapsedCode = {
  code: string;
  meaning: string;
  attribute: string | null;
  count: number;
};

export function collapseCodes(codes: DecodedCode[]): CollapsedCode[] {
  const out: CollapsedCode[] = [];
  // Group key is the visible identity of a row: the code, the glossary meaning,
  // and the protected-attribute tag. Two `C/F` rows with different meanings
  // (an edited glossary, re-extracted) are genuinely different lines.
  const seen = new Map<string, CollapsedCode>();
  for (const entry of codes) {
    const key = `${entry.code}\u0000${entry.meaning}\u0000${entry.attribute ?? ""}`;
    const existing = seen.get(key);
    if (existing) {
      existing.count += 1;
    } else {
      const collapsed: CollapsedCode = {
        code: entry.code,
        meaning: entry.meaning,
        attribute: entry.attribute,
        count: 1,
      };
      seen.set(key, collapsed);
      out.push(collapsed);
    }
  }
  return out;
}

export function flagged(row: Opportunity): boolean {
  return row.references_protected_attribute === true;
}

/** The single sex a job order's coded shorthand implies, or null.

 *  Mirrors `implied_sex` in the backend's `sourcing/preference.py`: reads the
 *  human-readable `meaning` of each detected code (the `attribute` column can't
 *  be used — `C/F` is filed under `race`), returns the sex only when every
 *  sex-bearing code agrees, and returns null on conflict, absence, or when no
 *  code names a sex. Used only to show an honest hint under the PlacementForm —
 *  the actual narrowing is done server-side, stamped on the run, and quoted in
 *  the shortlist's Safeguards banner. */
export function impliedSex(codes: DecodedCode[]): "female" | "male" | null {
  const implied = new Set<"female" | "male">();
  for (const entry of codes) {
    const meaning = entry.meaning ?? "";
    // `\bmale\b` does not fire inside "female"; female is tested first so its
    // longer spelling is claimed before male can match its tail.
    if (/\bfemale\b/i.test(meaning)) implied.add("female");
    else if (/\bmale\b/i.test(meaning)) implied.add("male");
  }
  if (implied.size !== 1) return null;
  return [...implied][0];
}

/** The distinct attributes referred to, in the order they were found. */
function attributesOf(codes: DecodedCode[]): string[] {
  const seen: string[] = [];
  for (const entry of codes) {
    if (entry.attribute && !seen.includes(entry.attribute)) seen.push(entry.attribute);
  }
  return seen;
}

/** The short mark, for a table cell or a panel heading. */
export function ProtectedBadge() {
  return (
    <span className="jo-protected">
      <span className="jo-protected-dot" aria-hidden="true" />
      <span>Protected attribute mentioned</span>
    </span>
  );
}

/** The long form, for the detail panel, where there is room to say what and
 *  whose words they are. */
export function DecodedCodes({ row }: { row: Opportunity }) {
  const codes = codesOf(row);
  const isFlagged = flagged(row);

  // Nothing found and nothing flagged is not worth a heading. The panel's
  // "Not mentioned" convention is for fields we look for in every email; this
  // section only exists when the email actually used shorthand.
  if (codes.length === 0 && !isFlagged) return null;

  // Collapsed: one line per distinct code+meaning, with a count when a code
  // recurred. Eight "JD" in one email reads as one "JD (×8)", not eight lines.
  const collapsed = collapseCodes(codes);
  const attributes = attributesOf(codes);

  return (
    <div className="jo-codes">
      <span className="row-k">Shorthand in this email</span>
      <p className="body jo-sub jo-codes-lede">
        The code is quoted from the email exactly as the sender wrote it. The meaning beside it is
        your glossary&rsquo;s definition of that code, not anything the sender said.
      </p>

      {collapsed.length === 0 ? (
        <p className="body muted jo-codes-none">Not mentioned</p>
      ) : (
        <ul className="jo-codes-list">
          {collapsed.map((entry) => (
            <li
              className="jo-code"
              key={`${entry.code}-${entry.meaning}-${entry.attribute ?? ""}`}
              data-protected={entry.attribute ? "yes" : undefined}
            >
              <code className="jo-code-lit">
                <Breakable text={entry.code} />
              </code>
              <span className="jo-code-arrow" aria-hidden="true">
                →
              </span>
              {/* Read aloud, the arrow is silent and the two halves would run
                  together as one phrase the sender never wrote. */}
              <span className="jo-code-mean">
                <span className="jo-sr">your glossary: </span>
                <Breakable text={entry.meaning} />
              </span>
              {entry.count > 1 && (
                <span className="jo-code-count" title={`${entry.count} occurrences in this email`}>
                  ×{entry.count}
                </span>
              )}
              {entry.attribute && (
                <span className="jo-code-attr">{attributeLabel(entry.attribute)}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {isFlagged && (
        <div className="jo-protected-note">
          <ProtectedBadge />
          <p className="body jo-sub">
            {attributes.length > 0
              ? `Shorthand in this email decodes, under your glossary, to ${list(attributes)}.`
              : "Shorthand in this email decodes, under your glossary, to a protected attribute."}{" "}
            The wording is the client&rsquo;s, kept as they sent it. Someone should read this job
            order and decide how to handle it.
          </p>
        </div>
      )}
    </div>
  );
}

/** "race and gender", "race, gender and age" — lower-cased, because these read
 *  as part of a sentence rather than as labels. */
function list(attributes: string[]): string {
  const words = attributes.map((name) => attributeLabel(name).toLowerCase());
  if (words.length === 1) return words[0];
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}
