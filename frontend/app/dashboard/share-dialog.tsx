"use client";

import { useEffect, useId, useMemo, useState } from "react";

import { useAuth } from "../auth";
import { Dialog } from "./dialog";
import { MemberPicker } from "./member-picker";
import { useMembers } from "./members";
import { type Opportunity } from "./opportunities";
import { Initials } from "./person";
import { listShares, shareOpportunity, unshare, type Share } from "./shares";

/**
 * Handing a job order to whoever can fill it.
 *
 * The headline interaction of the whole screen: a recruiter who cannot fill a
 * vacancy passes it to someone who can. There is no access level to choose,
 * because a share grants sight and only sight — the recipient reads the job
 * order and may pass it on again to a colleague they know, which is how work
 * travels down a chain to the right desk. Editing stays with the assignee.
 *
 * The broadcast is the one control that is not everyone's. Throwing another
 * recruiter's client work open to the whole office is a decision for whoever
 * is carrying it — the assignee — or for the agency owner. On an unassigned
 * job order there is no assignee at all, so only an owner may broadcast it;
 * that is the shipped API rule, which gates on the same check that refuses
 * unassigned rows and then falls back to the owner role.
 *
 * It is disabled with its reason showing rather than hidden. A control that
 * vanishes teaches nothing: the reader learns neither that broadcasting exists
 * nor what would earn it. Disabled-with-a-sentence turns a refusal into the
 * one thing they can act on — claim it.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

/** What removing somebody actually does. Said out loud because "unshare"
 *  reads like it might take their work with it, and a recruiter who fears
 *  that will simply leave stale access in place forever. */
const REMOVAL_NOTE =
  "Removing someone stops them seeing it from now on. Nothing they have done on this job order is deleted.";

function reasonNotToBroadcast(row: Opportunity): string {
  return row.assigned_user_id === null
    ? "Nobody is assigned to this job order yet, so there is no one whose call it is — claim it first, or ask an owner."
    : "Only the assigned recruiter or an agency owner can share this with the whole agency. You can still name colleagues above.";
}

export function ShareDialog({ row, onClose }: { row: Opportunity; onClose: () => void }) {
  const auth = useAuth();
  const members = useMembers();
  const titleId = useId();
  const noteId = useId();
  const broadcastId = useId();

  const [shares, setShares] = useState<Share[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [broadcast, setBroadcast] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    listShares(row.id).then(
      (items) => live && setShares(items),
      // A share list we cannot read is one broken panel, not a broken dialog:
      // naming a colleague still works, so the form stays up.
      () => live && setError("Couldn’t load who this is already shared with."),
    );
    return () => {
      live = false;
    };
  }, [row.id]);

  const named = useMemo(
    () => shares.filter((share) => share.scope === "user" && share.shared_with_user_id),
    [shares],
  );
  const broadcastShare = shares.find((share) => share.scope === "tenant") ?? null;

  // Already a recipient: offering them again would post a share that changes
  // nothing and notifies nobody.
  const exclude = useMemo(
    () => named.map((share) => share.shared_with_user_id as string),
    [named],
  );

  const nameOf = useMemo(() => {
    const byId = new Map(members.members.map((member) => [member.id, member.name]));
    return (id: string) => byId.get(id) ?? "A colleague";
  }, [members.members]);

  const signedIn = auth.status === "signed-in" ? auth.me.user : null;
  // Mirrors the server: the owner role, or the person actually holding it.
  // `assigned_user_id !== null` is not redundant with the comparison — an
  // unassigned row and a signed-out reader would otherwise both be `null`.
  const iMayBroadcast =
    signedIn !== null &&
    (signedIn.role === "owner" ||
      (row.assigned_user_id !== null && row.assigned_user_id === signedIn.id));

  const canSubmit = !busy && (broadcast ? iMayBroadcast : selected.length > 0);

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const trimmed = note.trim();
      await shareOpportunity(row.id, {
        scope: broadcast ? "tenant" : "user",
        user_ids: broadcast ? [] : selected,
        note: trimmed === "" ? null : trimmed,
      });
      setSelected([]);
      setNote("");
      setBroadcast(false);
      setDone(broadcast ? "Shared with the whole agency." : "Shared.");
      setShares(await listShares(row.id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "We could not share that just now.");
    } finally {
      setBusy(false);
    }
  }

  async function remove(share: Share) {
    setError(null);
    setDone(null);
    // Dropped from the list first: the row is gone the moment the server says
    // 204, and re-fetching only to render the same absence is a round trip the
    // reader waits through.
    try {
      await unshare(row.id, share.id);
      setShares((current) => current.filter((other) => other.id !== share.id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "We could not remove that just now.");
    }
  }

  return (
    <Dialog title="Share this job order" titleId={titleId} onClose={onClose} className="shr">
      <p className="body jo-sub">
        Whoever you name can read this job order and pass it on to someone else. They cannot edit
        it.
      </p>

      <MemberPicker
        selected={selected}
        onChange={setSelected}
        exclude={exclude}
        label="Share with"
      />

      <div className="shr-note">
        <label className="mp-label" htmlFor={noteId}>
          Note (optional)
        </label>
        <textarea
          id={noteId}
          className="input shr-note-box"
          rows={2}
          value={note}
          placeholder="Why this one is for them"
          onChange={(event) => setNote(event.target.value)}
        />
      </div>

      {/* Disabled, never hidden. See the note at the top of this file. */}
      <div className="shr-broadcast">
        <input
          id={broadcastId}
          type="checkbox"
          checked={broadcast}
          disabled={!iMayBroadcast}
          onChange={(event) => setBroadcast(event.target.checked)}
        />
        <label htmlFor={broadcastId}>Share with the whole agency</label>
      </div>
      {!iMayBroadcast && (
        <p className="body jo-sub shr-reason">{reasonNotToBroadcast(row)}</p>
      )}

      <div className="shr-actions">
        <button type="button" className="btn btn-primary" onClick={submit} disabled={!canSubmit}>
          {busy ? "Sharing…" : "Share"}
        </button>
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Done
        </button>
      </div>

      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
      {done && (
        <p className="body jo-sub" role="status">
          {done}
        </p>
      )}

      <div className="shr-current">
        <span className="row-k">Already shared with</span>
        {broadcastShare === null && named.length === 0 ? (
          <p className="body muted">Nobody yet.</p>
        ) : (
          <ul className="shr-list">
            {broadcastShare && (
              <li className="shr-row">
                <span>Everyone at the agency</span>
                <button
                  type="button"
                  className="btn btn-secondary shr-x"
                  onClick={() => void remove(broadcastShare)}
                >
                  Remove
                </button>
              </li>
            )}
            {named.map((share) => {
              const id = share.shared_with_user_id as string;
              const name = nameOf(id);
              return (
                <li key={share.id} className="shr-row">
                  <Initials name={name} seed={id} size={22} />
                  <span>{name}</span>
                  {share.note && <span className="muted shr-row-note">{share.note}</span>}
                  <button
                    type="button"
                    className="btn btn-secondary shr-x"
                    aria-label={`Remove ${name}`}
                    onClick={() => void remove(share)}
                  >
                    Remove
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        <p className="body jo-sub">{REMOVAL_NOTE}</p>
      </div>
    </Dialog>
  );
}
