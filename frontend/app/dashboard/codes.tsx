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

  const attributes = attributesOf(codes);

  return (
    <div className="jo-codes">
      <span className="row-k">Shorthand in this email</span>
      <p className="body jo-sub jo-codes-lede">
        The code is quoted from the email exactly as the sender wrote it. The meaning beside it is
        your glossary&rsquo;s definition of that code, not anything the sender said.
      </p>

      {codes.length === 0 ? (
        <p className="body muted jo-codes-none">Not mentioned</p>
      ) : (
        <ul className="jo-codes-list">
          {codes.map((entry) => (
            <li
              className="jo-code"
              key={`${entry.start_char}-${entry.end_char}-${entry.code}`}
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
