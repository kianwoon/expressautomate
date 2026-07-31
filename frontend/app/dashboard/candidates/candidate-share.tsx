"use client";

import { useCallback, useEffect, useId, useMemo, useState } from "react";

import { useAuth } from "../../auth";
import {
  canEditCandidate,
  declineCandidateAccess,
  deleteCandidateShare,
  grantCandidateAccess,
  listCandidateAccessRequests,
  listCandidateShares,
  shareCandidate,
  type Candidate,
  type CandidateAccessRequest,
  type CandidateShare,
} from "../candidates";
import { Dialog } from "../dialog";
import { MemberPicker } from "../member-picker";
import { useMembers } from "../members";
import { Initials } from "../person";

/**
 * Showing a candidate to a colleague, and answering somebody who asked.
 *
 * Structurally a sibling of `share-dialog.tsx`, deliberately: one sharing
 * idiom in this app rather than two, the same way `candidate_shares.py` is a
 * deliberate mirror of `opportunity_shares.py` on the server. Same two scopes,
 * same "a share grants sight and nothing else", same disabled-with-a-reason
 * broadcast.
 *
 * What has no equivalent on the job order side is the second half of this
 * file. A recruiter who meets a candidate a colleague already holds sees
 * nothing at all — the server 404s the row — so there has to be a way to ask,
 * and therefore a place where the person asked can answer. That is the inbox,
 * and without it a request sits pending forever while the asker believes it is
 * still open, waits, and asks again.
 *
 * allow-hardcode: the strings below are user-facing copy rendered to the page,
 * not a list anything is matched against.
 */

/** Said out loud because "remove" reads like it might take their work with
 *  it, and a recruiter who fears that leaves stale access in place forever. */
const REMOVAL_NOTE =
  "Removing someone stops them seeing this candidate from now on. Nothing they have logged — a WhatsApp message, a call — is deleted.";

const BROADCAST_REASON =
  "Only the recruiter who holds this candidate, or an agency owner, can show them to everyone. You can still name colleagues above.";

const QUEUE_REASON =
  "Nobody holds this candidate yet, so there is no one whose call it is — claim them first, or ask an owner.";

export function CandidateShareDialog({
  row,
  onClose,
}: {
  row: Candidate;
  onClose: () => void;
}) {
  const auth = useAuth();
  const members = useMembers();
  const titleId = useId();
  const noteId = useId();
  const broadcastId = useId();

  const [shares, setShares] = useState<CandidateShare[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [broadcast, setBroadcast] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    listCandidateShares(row.id).then(
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
  // The server's rule, mirrored rather than discovered by 403: the broadcast
  // is the owner's call, and `can_edit_candidate` is what the route checks.
  const iMayBroadcast = signedIn !== null && canEditCandidate(row, signedIn);
  const unclaimed = row.owner_id === null;

  const canSubmit = !busy && (broadcast ? iMayBroadcast : selected.length > 0);

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const trimmed = note.trim();
      await shareCandidate(row.id, {
        scope: broadcast ? "tenant" : "user",
        user_ids: broadcast ? [] : selected,
        note: trimmed === "" ? null : trimmed,
      });
      setSelected([]);
      setNote("");
      setBroadcast(false);
      setDone(broadcast ? "Shared with the whole agency." : "Shared.");
      setShares(await listCandidateShares(row.id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "We could not share that just now.");
    } finally {
      setBusy(false);
    }
  }

  async function remove(share: CandidateShare) {
    setError(null);
    setDone(null);
    try {
      await deleteCandidateShare(row.id, share.id);
      setShares((current) => current.filter((other) => other.id !== share.id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "We could not remove that just now.");
    }
  }

  return (
    <Dialog title={`Share ${row.full_name}`} titleId={titleId} onClose={onClose} className="shr">
      <p className="body jo-sub">
        Whoever you name can read this candidate’s record and pass it on to someone else. They
        cannot edit it, and they cannot hand the candidate over.
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

      {/* Disabled, never hidden — the same rule as the job order dialog, and
          for the same reason: a control that vanishes teaches the reader
          neither that broadcasting exists nor what would earn it. */}
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
        <p className="body jo-sub shr-reason">{unclaimed ? QUEUE_REASON : BROADCAST_REASON}</p>
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

/**
 * Colleagues waiting on an answer from you.
 *
 * Renders NOTHING when there is nothing pending — not an empty card, not a
 * zero. This sits above a working list, and a permanent "0 requests" panel is
 * a line of furniture every recruiter learns to look past, which is exactly
 * the wrong habit for the one screen that must be noticed when it does have
 * something in it.
 *
 * It names the asker, not the candidate, and that asymmetry is the server's:
 * the inbox route returns no candidate name, because naming the row here would
 * leak through the inbox what the request route itself refuses to return. The
 * owner already knows who they hold.
 */
export function AccessRequestInbox({ onResolved }: { onResolved: () => void }) {
  const members = useMembers();
  const [requests, setRequests] = useState<CandidateAccessRequest[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    // A failed read is swallowed to an empty inbox on purpose. This is an
    // extra panel over a working candidate list; an error banner here for a
    // flaky request would report the page broken when it is not.
    listCandidateAccessRequests("pending").then(setRequests, () => setRequests([]));
  }, []);

  useEffect(load, [load]);

  const nameOf = useMemo(() => {
    const byId = new Map(members.members.map((member) => [member.id, member.name]));
    return (id: string) => byId.get(id) ?? "A colleague";
  }, [members.members]);

  async function answer(request: CandidateAccessRequest, grant: boolean) {
    if (busyId) return;
    setBusyId(request.id);
    setError(null);
    try {
      if (grant) {
        await grantCandidateAccess(request.candidate_id, request.id);
      } else {
        await declineCandidateAccess(request.candidate_id, request.id);
      }
      setRequests((current) => current.filter((other) => other.id !== request.id));
      // Granting writes a share, which changes who can see what — the list
      // behind this is now a row wider for somebody. Declining changes
      // nothing visible, but re-reading once costs less than two code paths.
      onResolved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "We could not answer that just now.");
    } finally {
      setBusyId(null);
    }
  }

  if (requests.length === 0) return null;

  return (
    <div className="card cand-inbox" role="region" aria-label="Requests to see your candidates">
      <span className="row-k">
        {requests.length === 1
          ? "A colleague is asking to see one of your candidates"
          : `${requests.length} colleagues are asking to see your candidates`}
      </span>
      <p className="body jo-sub">
        They met someone you already hold and cannot see the record. Saying no is a real answer —
        an unanswered request leaves them waiting, and then asking again.
      </p>
      <ul className="shr-list">
        {requests.map((request) => {
          const asker = nameOf(request.requested_by_user_id);
          return (
            <li key={request.id} className="shr-row">
              <Initials name={asker} seed={request.requested_by_user_id} size={22} />
              <span>{asker}</span>
              {request.note && <span className="muted shr-row-note">{request.note}</span>}
              <button
                type="button"
                className="btn btn-primary shr-x"
                aria-label={`Share with ${asker}`}
                disabled={busyId !== null}
                onClick={() => void answer(request, true)}
              >
                Share
              </button>
              <button
                type="button"
                className="btn btn-secondary shr-x"
                aria-label={`Decline ${asker}`}
                disabled={busyId !== null}
                onClick={() => void answer(request, false)}
              >
                Decline
              </button>
            </li>
          );
        })}
      </ul>
      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
