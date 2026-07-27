"use client";

import { Fragment } from "react";

/**
 * A long value with break opportunities at the places a reader expects one.
 *
 * Lifted out of the dashboard page when the job-orders table needed it too —
 * a second copy would have drifted, and the reason it exists is subtle enough
 * that the drift would not have been obvious.
 *
 * `.row` values carry `overflow-wrap: anywhere`, which stops an address
 * spilling past the card but breaks it wherever the line happens to run out:
 * "wiserly@hotmail.co / m" — which reads as a typo in the user's own address
 * rather than as wrapping. `anywhere` is still the right backstop; what it was
 * missing is somewhere better to break.
 *
 * `<wbr>` supplies exactly that. Browsers prefer an explicit opportunity over
 * an arbitrary one, so the address now breaks after the `@` or before the
 * `.com` and falls back to mid-token only for something with no boundaries at
 * all. It inserts no character: copying the text still yields the address.
 */
export function Breakable({ text }: { text: string }) {
  const pieces = splitAtSeparators(text);
  return (
    <>
      {pieces.map((piece, i) => (
        <Fragment key={i}>
          {piece}
          {i < pieces.length - 1 && <wbr />}
        </Fragment>
      ))}
    </>
  );
}

/**
 * Split after `@ : ; ,` and before `. / - _` — the separators an address, URL
 * or id is likely to carry, cut so the delimiter stays with the part a reader
 * scans for.
 *
 * A hand-written scan rather than a regex with lookbehind. Lookbehind is a
 * *parse-time* error on Safari before 16.4, which would not degrade this
 * component — it would throw while loading the bundle and take the whole
 * dashboard with it. No wrapping nicety is worth that.
 */
export function splitAtSeparators(text: string): string[] {
  const AFTER = "@:;,";
  const BEFORE = "./-_";
  const pieces: string[] = [];
  let current = "";

  for (const char of text) {
    if (current && BEFORE.includes(char)) {
      pieces.push(current);
      current = "";
    }
    current += char;
    if (AFTER.includes(char)) {
      pieces.push(current);
      current = "";
    }
  }
  if (current) pieces.push(current);
  return pieces;
}
