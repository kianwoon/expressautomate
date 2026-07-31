"use client";

import { useEffect, useId, useRef, useState } from "react";

import { CLIENTS_PATH } from "../api";

/**
 * Type-to-search over the agency's clients.
 *
 * Not `MemberPicker`, and not a `<select>`. The members list is 3-50 people
 * and loads once; clients are paginated and an agency accumulates hundreds of
 * them, so a preloaded dropdown would either be a very long list or a quietly
 * truncated one. The query goes to the server as `?q=`.
 *
 * Leaving it empty is an ordinary outcome, not a failed selection: a company
 * nobody has recorded yet has no row to point at, which is exactly what a
 * nullable `client_id` is for. So typing without choosing a match sends no
 * client at all rather than blocking the save — and the text is not smuggled
 * into the client field either. It goes in `company_name_raw`, where an
 * unrecorded company name belongs.
 *
 * It lives on its own because two screens need it — the manual job-order form
 * and the job-order detail panel — and a second copy is a second debounce, a
 * second race rule and a second set of bugs.
 *
 * allow-hardcode: the strings below are user-facing labels and copy rendered
 * to the page, not a list anything is matched against.
 */

/** Waited out after the last keystroke before the client search reaches the
 *  server. The same value `useOpportunities` uses for its own search box —
 *  live enough to feel immediate, long enough that a five-letter company is
 *  one request rather than five. */
const CLIENT_SEARCH_DEBOUNCE_MS = 300;

/** How many matches the picker offers. A short list is scannable; a recruiter
 *  who cannot see the one they want types another letter, which is cheaper
 *  than scrolling. */
const CLIENT_SEARCH_LIMIT = 8;

/** Only what the picker draws. Deliberately not `Client` from `clients.ts`:
 *  this reads two fields of a list row and typing it as the whole record
 *  would suggest the rest is available here. */
export type ClientMatch = { id: string; name: string };

/** The line under the field on the manual job-order form, and the default
 *  because that is the screen this component was written for. It names the
 *  form's own Company input, so a screen without one has to say something
 *  else — see the `hint` prop. */
const DEFAULT_HINT =
  "Leave this empty if the company is not on our list yet. The name still goes in Company.";

export function ClientSearch({
  value,
  onChange,
  label,
  hint = DEFAULT_HINT,
}: {
  value: ClientMatch | null;
  onChange: (client: ClientMatch | null) => void;
  label: string;
  /** What the line under the field says. Overridable because the default
   *  points at the manual form's Company input, and the detail panel has no
   *  such field — telling a recruiter to type the name somewhere that is not
   *  on the screen is worse than saying nothing. */
  hint?: string;
}) {
  const inputId = useId();
  const listId = useId();
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<ClientMatch[]>([]);
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  // Every search takes a ticket and only the newest may write, the same rule
  // the lists use: "sun" and "sunr" are two requests and the shorter one can
  // land second, putting the wider set of matches under the narrower text.
  const generation = useRef(0);

  useEffect(() => {
    const term = query.trim();
    if (term.length === 0) {
      setMatches([]);
      return;
    }
    const timer = setTimeout(async () => {
      const mine = ++generation.current;
      try {
        const params = new URLSearchParams({ q: term, limit: String(CLIENT_SEARCH_LIMIT) });
        const res = await fetch(`${CLIENTS_PATH}?${params.toString()}`, {
          credentials: "include",
          headers: { Accept: "application/json" },
        });
        if (mine !== generation.current) return;
        if (!res.ok) {
          setMatches([]);
          setOpen(false);
          setFailed(true);
          return;
        }
        const body = (await res.json()) as { items?: ClientMatch[] };
        if (mine !== generation.current) return;
        setMatches(body.items ?? []);
        setOpen(true);
        setFailed(false);
      } catch {
        // Quiet, not silent. An error banner over a client lookup would report
        // a problem with a job order that is fine, so this is a line under the
        // field: nothing is blocked, but the recruiter is not left reading an
        // empty list as "no such client".
        if (mine === generation.current) {
          setMatches([]);
          setOpen(false);
          setFailed(true);
        }
      }
    }, CLIENT_SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  return (
    <div className="jo-form-field jo-client-picker">
      <label htmlFor={inputId}>{label}</label>
      <input
        id={inputId}
        className="jo-search"
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        autoComplete="off"
        value={value ? value.name : query}
        placeholder="Search, or leave empty"
        onChange={(event) => {
          // Editing after a selection un-selects it. The text no longer names
          // the client that was chosen, and keeping the id would file the job
          // order under a company the recruiter has visibly typed away from.
          if (value) onChange(null);
          setQuery(event.target.value);
        }}
        // Deferred, like `MemberPicker`'s: a blur fires before the click that
        // caused it, so closing immediately would close the list out from
        // under the match being chosen.
        onBlur={() => window.setTimeout(() => setOpen(false), 0)}
        onFocus={() => matches.length > 0 && setOpen(true)}
      />
      {open && matches.length > 0 && !value && (
        <ul id={listId} className="jo-client-matches" role="listbox">
          {matches.map((match) => (
            <li key={match.id}>
              <button
                type="button"
                role="option"
                aria-selected={false}
                onClick={() => {
                  onChange(match);
                  setOpen(false);
                }}
              >
                {match.name}
              </button>
            </li>
          ))}
        </ul>
      )}
      {failed && (
        <p className="jo-form-hint" role="status">
          Could not search clients just now. You can still leave this empty.
        </p>
      )}
      <p className="jo-form-hint">{hint}</p>
    </div>
  );
}
