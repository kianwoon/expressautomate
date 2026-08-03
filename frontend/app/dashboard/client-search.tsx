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
 * The manual form has no separate Company field any more — this is the one
 * place the company name is typed. Picking a match sets `client_id`; typing a
 * name that matches nothing is not an error, it is a new client, created on
 * save. `onQueryChange` exists so a caller that cares about the raw text (the
 * manual form does, for `company_name_raw`) can have it, without forcing that
 * bookkeeping on the detail panel, which does not.
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

/** The line under the field. The default now describes this component's own
 *  behaviour — pick a match, or type a name that has none and it becomes a
 *  new client on save — rather than pointing at a separate Company input,
 *  because the manual form no longer has one; this field is where the
 *  company name is typed. */
const DEFAULT_HINT = "Pick a client from the list, or just type the name — it'll be added when you save.";

/** Told the client will be created from what is on screen right now: typed
 *  text with no match picked. Shown in the dropdown's empty state rather
 *  than as a confirmation step — the user already chose silent creation on
 *  save, so this is a light note, not a gate. */
function newClientNote(query: string): string {
  return `No match for "${query}" — it will be added as a new client when you save.`;
}

export function ClientSearch({
  value,
  onChange,
  label,
  hint = DEFAULT_HINT,
  placeholder = "Search, or leave empty",
  onQueryChange,
}: {
  value: ClientMatch | null;
  onChange: (client: ClientMatch | null) => void;
  label: string;
  /** What the line under the field says. Overridable because the default now
   *  describes generic add-on-save behaviour, and a caller with a narrower
   *  story (the detail panel: link to an existing client) may want its own
   *  words. */
  hint?: string;
  /** Shown before anything is typed. */
  placeholder?: string;
  /** Told the raw text as it is typed, selection or not, and again with the
   *  matched name when a match is picked. The manual job-order form needs
   *  this: `company_name_raw` must always carry what was typed. A caller that
   *  does not pass it (the detail panel) simply is not told — it never
   *  needed the raw text, only the picked client. */
  onQueryChange?: (raw: string) => void;
}) {
  const inputId = useId();
  const listId = useId();
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<ClientMatch[]>([]);
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  // True only once a request for the CURRENT text has completed — not while
  // the debounce is pending, not while the request is in flight. The
  // "will be added as a new client" note reads it: without this, `matches`
  // is briefly `[]` on every keystroke (this state's own initial value, or
  // the previous term's leftovers cleared below) and the note would flash
  // for a company that is about to be found, telling the recruiter they are
  // about to create a duplicate when they are not.
  const [searched, setSearched] = useState(false);
  // Every search takes a ticket and only the newest may write, the same rule
  // the lists use: "sun" and "sunr" are two requests and the shorter one can
  // land second, putting the wider set of matches under the narrower text.
  // `searched` is set from inside the same `mine === generation.current`
  // check as `matches`, so a stale reply that loses the check cannot mark a
  // newer, still-pending search as settled either.
  const generation = useRef(0);

  useEffect(() => {
    const term = query.trim();
    if (term.length === 0) {
      setMatches([]);
      setSearched(false);
      return;
    }
    setSearched(false);
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
          setSearched(false);
          return;
        }
        const body = (await res.json()) as { items?: ClientMatch[] };
        if (mine !== generation.current) return;
        setMatches(body.items ?? []);
        setOpen(true);
        setFailed(false);
        setSearched(true);
      } catch {
        // Quiet, not silent. An error banner over a client lookup would report
        // a problem with a job order that is fine, so this is a line under the
        // field: nothing is blocked, but the recruiter is not left reading an
        // empty list as "no such client".
        if (mine === generation.current) {
          setMatches([]);
          setOpen(false);
          setFailed(true);
          setSearched(false);
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
        placeholder={placeholder}
        onChange={(event) => {
          // Editing after a selection un-selects it. The text no longer names
          // the client that was chosen, and keeping the id would file the job
          // order under a company the recruiter has visibly typed away from.
          if (value) onChange(null);
          const next = event.target.value;
          setQuery(next);
          onQueryChange?.(next);
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
                  onQueryChange?.(match.name);
                  setOpen(false);
                }}
              >
                {match.name}
              </button>
            </li>
          ))}
        </ul>
      )}
      {searched && open && matches.length === 0 && !failed && !value && query.trim().length > 0 && (
        <p className="jo-form-hint" role="status">
          {newClientNote(query.trim())}
        </p>
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
