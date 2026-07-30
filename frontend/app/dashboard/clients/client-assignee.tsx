"use client";

import { useState } from "react";

import { useAuth } from "../../auth";
import type { Client, Collaborator } from "../clients";
import { addCollaborator, ApiError, removeCollaborator, setClientAssignee } from "../clients";
import { MemberSelect } from "../member-picker";
import { useMembers } from "../members";
import { Initials } from "../person";

/**
 * Which recruiter looks after this client, and who else knows the account.
 *
 * This control is what makes routing work: a job order arriving by email is
 * assigned to whoever holds its client, so a client with nobody on it is a
 * job order that lands on nobody's desk.
 *
 * Two rules here are the shipped API's rather than this file's invention.
 *
 * Who may hand an account on — the owner, whoever holds it now, or anyone at
 * all when nobody holds it — is `_require_may_reassign` in
 * `app/api/clients.py`, which answers everyone else with a 403. The control is
 * *hidden* in exactly those cases rather than shown and refused. That differs
 * from `ShareDialog`, which disables its broadcast with the reason showing:
 * there the rule is worth learning, here the reader is simply not this
 * client's recruiter and there is nothing for them to learn or do.
 *
 * And "open" job orders do not exist. The request field is
 * `move_open_opportunities` because the API fixed that name, but `Opportunity`
 * carries only `review_status` and `quality_state` — neither says filled,
 * closed or lost. So no string a recruiter reads may claim it. What moves is
 * every job order for this client currently held by the outgoing recruiter,
 * and the server returns the count so this can say how many rather than
 * moving a dozen of them in silence.
 */

const OWNER_ROLE = "owner";

function moveSummary(moved: number, name: string | null, clientName: string): string {
  if (moved === 0) {
    return name === null
      ? `Nobody looks after ${clientName} now.`
      : `${clientName} is now looked after by ${name}.`;
  }
  // Deliberately not "open job orders" — see the note at the top of the file.
  const orders = moved === 1 ? "1 job order" : `${moved} job orders`;
  return name === null
    ? `${orders} are now unassigned.`
    : `${orders} moved to ${name}.`;
}

export function ClientAssignee({
  client,
  onChanged,
}: {
  client: Client;
  onChanged: () => void;
}) {
  const auth = useAuth();
  const staff = useMembers();

  // Absent and null mean the same thing: `_serialize` does not emit the field
  // yet, and treating `undefined` as "nobody" would offer to reassign a client
  // that is in fact already held.
  const held = client.assigned_user_id ?? null;

  const [pending, setPending] = useState<string | null>(held);
  const [move, setMove] = useState(true);
  const [collaborators, setCollaborators] = useState<Collaborator[]>(client.collaborators ?? []);
  const [pick, setPick] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  /** A name from the staff list, falling back to whatever the record carried.
   *  Never invented (§15) — `null` where nothing knows it. */
  function nameOf(id: string | null): string | null {
    if (id === null) return null;
    const member = staff.members.find((m) => m.id === id);
    if (member) return member.name;
    if (id === held && client.assignee_name) return client.assignee_name;
    return collaborators.find((c) => c.user_id === id)?.name ?? null;
  }

  const me = auth.status === "signed-in" ? auth.me.user : null;
  const mayReassign =
    me !== null && (me.role === OWNER_ROLE || held === null || held === me.id);

  async function run(work: () => Promise<void>, fallback: string) {
    setBusy(true);
    setFailed(null);
    try {
      await work();
    } catch (error) {
      setFailed(error instanceof ApiError ? error.message : fallback);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    await run(async () => {
      const result = await setClientAssignee(client.id, pending, move);
      setNote(moveSummary(result.opportunities_moved, nameOf(pending), client.name));
      onChanged();
    }, "We could not change who looks after this client just now.");
  }

  async function add() {
    const id = pick;
    if (id === null) return;
    await run(async () => {
      await addCollaborator(client.id, id);
      // Idempotent server-side, so a repeat is a success rather than an error
      // — and must not become a second row here either.
      setCollaborators((current) =>
        current.some((c) => c.user_id === id)
          ? current
          : [...current, { user_id: id, name: nameOf(id) }],
      );
      setPick(null);
    }, "We could not add that colleague just now.");
  }

  async function drop(id: string) {
    await run(async () => {
      await removeCollaborator(client.id, id);
      setCollaborators((current) => current.filter((c) => c.user_id !== id));
    }, "We could not remove that colleague just now.");
  }

  return (
    <section className="cl-assignee" aria-label="Who looks after this client">
      <div className="cl-facts-head">
        <span className="eyebrow">Looked after by</span>
      </div>

      {mayReassign ? (
        <>
          <MemberSelect value={pending} onChange={setPending} allowNone label="Looked after by" />

          <label className="cl-assignee-move">
            <input
              type="checkbox"
              checked={move}
              disabled={busy}
              onChange={(e) => setMove(e.target.checked)}
            />
            {/* A client changing hands normally means the work changes hands,
                so this starts ticked — but it stays a choice, because the
                outgoing recruiter may be mid-placement on one of them. */}
            <span>Move this client&apos;s job orders to them</span>
          </label>

          <button
            type="button"
            className="btn btn-secondary cl-assignee-save"
            disabled={busy || pending === held}
            onClick={save}
          >
            {busy ? "Saving…" : "Save"}
          </button>
        </>
      ) : (
        <p className="cl-assignee-held">{nameOf(held) ?? "Nobody yet"}</p>
      )}

      {note && (
        <p className="cl-assignee-note" role="status">
          {note}
        </p>
      )}
      {failed && (
        <p className="cl-assignee-failed" role="alert">
          {failed}
        </p>
      )}

      <div className="cl-facts-head cl-collab-head">
        <span className="eyebrow">Who else knows this account</span>
      </div>
      {/* Said plainly, or the list below reads like a share. It is not one:
          `ClientCollaborator` grants nothing, and cover that needs sight of
          the work is an explicit share or a reassignment. */}
      <p className="muted cl-collab-note">
        Naming someone here grants no access to this client&apos;s job orders. It only records
        who else knows the account.
      </p>

      {collaborators.length > 0 && (
        <ul className="cl-collab-list">
          {collaborators.map((collaborator) => {
            const name = nameOf(collaborator.user_id) ?? "Someone who has left";
            return (
              <li key={collaborator.user_id} className="cl-collab">
                <Initials name={name} seed={collaborator.user_id} size={20} />
                <span>{name}</span>
                {mayReassign && (
                  <button
                    type="button"
                    className="mp-chip-x"
                    aria-label={`Remove ${name}`}
                    disabled={busy}
                    onClick={() => drop(collaborator.user_id)}
                  >
                    ×
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {mayReassign && (
        <div className="cl-collab-add">
          <MemberSelect
            value={pick}
            onChange={setPick}
            label="Add a colleague who also knows this account"
          />
          <button
            type="button"
            className="btn btn-secondary"
            disabled={busy || pick === null}
            onClick={add}
          >
            Add
          </button>
        </div>
      )}
    </section>
  );
}
