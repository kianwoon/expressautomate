"use client";

import { useId, useMemo, useRef, useState } from "react";

import { useAuth } from "../auth";
import { useMembers, type Member } from "./members";
import { Initials } from "./person";

/**
 * Naming a colleague: once as a multi-select (sharing) and once as a single
 * select (assignment).
 *
 * Both read `useAuth()` themselves rather than taking the caller's id as a
 * prop. Excluding yourself is not a decision each screen gets to make: sharing
 * with yourself is a no-op the API silently skips, and assigning a job order to
 * yourself has its own button. A prop is a thing a call site can forget; a hook
 * call inside the component is not.
 *
 * `useAuth()` is a state union, so there is a stretch where the answer to "who
 * am I" does not exist yet. Both controls are disabled until it does, rather
 * than listing everyone and then quietly dropping a row — that correction is
 * one frame of the reader seeing their own name, which looks like a bug and
 * costs a click if they were fast.
 */

/** Only "owner" is worth saying. Everyone else is a recruiter, and a badge on
 *  every row is a badge that says nothing. */
function roleLabel(member: Member): string | null {
  return member.role === "owner" ? "Owner" : null;
}

function matches(member: Member, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return (
    member.name.toLowerCase().includes(needle) || member.email.toLowerCase().includes(needle)
  );
}

/** Who this control may offer: the agency, less the caller, less anyone the
 *  caller has ruled out. Returns `null` while the caller is still unknown, so
 *  the two "not ready" reasons stay one check at every use site. */
function useOfferable(exclude?: string[]): { members: Member[] | null; message?: string } {
  const auth = useAuth();
  const list = useMembers();
  const excluded = useMemo(() => new Set(exclude ?? []), [exclude]);

  if (list.status === "unreadable") return { members: null, message: list.message };
  if (auth.status !== "signed-in" || list.status !== "ready") return { members: null };

  const me = auth.me.user.id;
  return { members: list.members.filter((m) => m.id !== me && !excluded.has(m.id)) };
}

export function MemberPicker({
  selected,
  onChange,
  exclude,
  label,
}: {
  selected: string[];
  onChange: (ids: string[]) => void;
  exclude?: string[];
  label: string;
}) {
  const { members, message } = useOfferable(exclude);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  // Which row the arrow keys are sitting on. -1 is "none yet", so the first
  // ArrowDown lands on the first row rather than skipping it.
  const [active, setActive] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const baseId = useId();
  const listId = `${baseId}-list`;
  const inputId = `${baseId}-input`;

  const chosen = useMemo(() => new Set(selected), [selected]);
  const byId = useMemo(() => new Map((members ?? []).map((m) => [m.id, m])), [members]);
  const options = useMemo(
    () => (members ?? []).filter((m) => !chosen.has(m.id) && matches(m, query)),
    [members, chosen, query],
  );

  const disabled = members === null;
  const showList = open && !disabled && options.length > 0;

  function add(id: string) {
    onChange([...selected, id]);
    setQuery("");
    setActive(-1);
    inputRef.current?.focus();
  }

  function remove(id: string) {
    onChange(selected.filter((other) => other !== id));
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      if (!open) return;
      // Stopped here so an Escape aimed at the list does not also close the
      // dialog this picker usually lives in — one key, one thing.
      event.stopPropagation();
      setOpen(false);
      setActive(-1);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (options.length === 0) return;
      setOpen(true);
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((current) => {
        const next = current + step;
        if (next < 0) return options.length - 1;
        if (next >= options.length) return 0;
        return next;
      });
      return;
    }
    if (event.key === "Enter") {
      if (!showList || active < 0 || active >= options.length) return;
      // Otherwise this submits the form the picker sits in, with the row the
      // recruiter was aiming at unchosen.
      event.preventDefault();
      add(options[active].id);
    }
  }

  return (
    <div className="mp">
      <label className="mp-label" htmlFor={inputId}>
        {label}
      </label>

      {selected.length > 0 && (
        <ul className="mp-chips">
          {selected.map((id) => {
            const member = byId.get(id);
            const name = member?.name ?? id;
            return (
              <li key={id} className="mp-chip">
                <Initials name={name} seed={id} size={20} />
                <span>{name}</span>
                <button
                  type="button"
                  className="mp-chip-x"
                  aria-label={`Remove ${name}`}
                  onClick={() => remove(id)}
                >
                  ×
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <input
        id={inputId}
        ref={inputRef}
        type="text"
        className="input mp-input"
        role="combobox"
        autoComplete="off"
        aria-expanded={showList}
        aria-controls={listId}
        aria-activedescendant={
          showList && active >= 0 ? `${baseId}-opt-${options[active].id}` : undefined
        }
        disabled={disabled}
        value={query}
        placeholder="Search colleagues"
        onFocus={() => !disabled && setOpen(true)}
        // A blur that lands on an option must not unmount it before the click
        // registers, so the close is deferred by one tick rather than run now.
        onBlur={() => window.setTimeout(() => setOpen(false), 0)}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(-1);
          setOpen(true);
        }}
        onKeyDown={onKeyDown}
      />

      {message && (
        <p className="mp-note" role="status">
          {message}
        </p>
      )}

      {showList && (
        <ul className="mp-list" id={listId} role="listbox" aria-label={label}>
          {options.map((member, index) => {
            const role = roleLabel(member);
            return (
              <li
                key={member.id}
                id={`${baseId}-opt-${member.id}`}
                role="option"
                className={`mp-option${index === active ? " is-active" : ""}`}
                aria-selected={index === active}
                // mousedown, not click: the input's blur fires first otherwise
                // and the row is gone before the button comes back up.
                onMouseDown={(e) => {
                  e.preventDefault();
                  add(member.id);
                }}
                onMouseEnter={() => setActive(index)}
              >
                <Initials name={member.name} seed={member.id} size={24} />
                <span className="mp-option-name">{member.name}</span>
                {role && <span className="mp-role">{role}</span>}
                <span className="mp-option-email">{member.email}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function MemberSelect({
  value,
  onChange,
  allowNone,
  label,
}: {
  value: string | null;
  onChange: (id: string | null) => void;
  allowNone?: boolean;
  label: string;
}) {
  const { members, message } = useOfferable();
  const baseId = useId();
  const selectId = `${baseId}-select`;

  const list = members ?? [];
  // Whoever is already assigned stays on the list even if they have since left
  // the agency — or are the caller, who can hold an assignment without being
  // offerable. Dropping them would make the select read "nobody" and the next
  // save would quietly mean it.
  const missing = value !== null && !list.some((m) => m.id === value);

  return (
    <div className="mp">
      <label className="mp-label" htmlFor={selectId}>
        {label}
      </label>
      <select
        id={selectId}
        className="input"
        disabled={members === null}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      >
        {(allowNone || value === null) && (
          <option value="">{allowNone ? "Nobody" : "Choose a colleague"}</option>
        )}
        {missing && <option value={value}>Someone who has left</option>}
        {list.map((member) => {
          const role = roleLabel(member);
          return (
            <option key={member.id} value={member.id}>
              {role ? `${member.name} · ${role}` : member.name}
            </option>
          );
        })}
      </select>
      {message && (
        <p className="mp-note" role="status">
          {message}
        </p>
      )}
    </div>
  );
}
