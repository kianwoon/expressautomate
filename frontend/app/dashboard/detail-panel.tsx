"use client";

import { useState } from "react";

import { useAuth } from "../auth";
import { Breakable } from "../breakable";
import { DecodedCodes, ProtectedBadge, flagged } from "./codes";
import { Salary, Value, day } from "./format";
import { PlacementForm } from "./job-order-placement";
import { Shortlist } from "./job-orders-sourcing";
import { MemberSelect } from "./member-picker";
import {
  FORBIDDEN_MESSAGE,
  GONE_MESSAGE,
  type MutationResult,
  type Opportunity,
} from "./opportunities";
import { Initials } from "./person";
import { QualityNote, ReviewBadge } from "./quality";

/**
 * One job order in full, beside the list.
 *
 * The table can only ever show the short fields; requirements and description
 * are paragraphs, and clamping them to four lines in a cell was always a
 * compromise. Here they are simply shown. That is the point of the split: the
 * list stays scannable because the long text has somewhere else to be.
 *
 * The panel never invents a heading for a field the email did not mention. It
 * shows the field and says "Not mentioned" — an absence a recruiter can see is
 * useful, an absence that is silently omitted looks like a field we do not
 * extract.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

type Ownership = {
  onClaim: (id: string) => Promise<MutationResult>;
  onAssign: (id: string, userId: string | null) => Promise<MutationResult>;
  /** The row went out from under the open panel — a share withdrawn, or an
   *  owner reassigning. The panel closes itself; the list is told so it can
   *  drop the selection rather than reopen what is gone. */
  onVanished: (id: string) => void;
};

/**
 * The 403 message depends on the row, not only on the status.
 *
 * The server says the same thing either way — this job order is not yours to
 * move. But on a row nobody was ever assigned, "shared with you, not assigned
 * to you" is simply untrue, and it sends the reader looking for a colleague
 * who does not exist. The honest sentence there names the way out: claim it.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */
function forbiddenMessage(row: Opportunity): string {
  return row.assigned_user_id
    ? "This job order is shared with you, not assigned to you."
    : "Claim this job order before editing it.";
}

export function DetailPanel({
  row,
  onReview,
  onClaim,
  onAssign,
  onVanished,
}: {
  row: Opportunity | null;
  onReview: (id: string, reviewed: boolean) => Promise<string | null>;
} & Ownership) {
  if (!row) {
    return (
      <aside className="card jo-detail" aria-label="Job order details">
        <span className="eyebrow">Details</span>
        <p className="body jo-detail-empty">
          Select a job order to read it in full — everything the email said, including the parts
          too long for the table.
        </p>
      </aside>
    );
  }

  // Keyed on the row id so the pending state and any error reset when the
  // selection moves. Without the key, an error from marking one row reviewed
  // would still be sitting under the next row someone clicked.
  return (
    <Detail
      key={row.id}
      row={row}
      onReview={onReview}
      onClaim={onClaim}
      onAssign={onAssign}
      onVanished={onVanished}
    />
  );
}

function Detail({
  row,
  onReview,
  onClaim,
  onAssign,
  onVanished,
}: {
  row: Opportunity;
  onReview: (id: string, reviewed: boolean) => Promise<string | null>;
} & Ownership) {
  const auth = useAuth();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reviewed = row.review_status === "reviewed";

  // Ownership keeps its own pending flag and its own message. Sharing them
  // with the review toggle would grey out a button whose action is unrelated,
  // and would put a claim failure under the sentence about review state.
  const [moving, setMoving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [gone, setGone] = useState(false);

  // The panel's own copy of the placement fields, so a save is reflected
  // immediately without waiting for the next page fetch to bring `row` back
  // around. Reset by the `key={row.id}` this component is mounted under, the
  // same trick `saving`/`error` above rely on.
  const [placement, setPlacement] = useState(row);

  async function toggle() {
    if (saving) return;
    setSaving(true);
    setError(null);
    setError(await onReview(row.id, !reviewed));
    setSaving(false);
  }

  /** Runs one ownership move and decides what the reader is left looking at.
   *
   * Three outcomes, not two. A 404 is not an error to display beside the
   * fields — the fields are stale the moment it arrives — so the panel closes
   * and reports upward. A 403 is re-worded against the row. Everything else is
   * the sentence the API already chose; nothing here re-derives copy. */
  async function move(run: () => Promise<MutationResult>) {
    if (moving) return;
    setMoving(true);
    setNotice(null);
    const result = await run();
    if (result.ok) {
      setMoving(false);
      return;
    }
    if (result.message === GONE_MESSAGE) {
      setGone(true);
      onVanished(row.id);
      return;
    }
    setNotice(result.message === FORBIDDEN_MESSAGE ? forbiddenMessage(row) : result.message);
    setMoving(false);
  }

  if (gone) {
    // One plain line. Stale fields under a red banner invite acting on a job
    // order that is no longer there; a blank panel reads as a crash.
    return (
      <aside className="card jo-detail" aria-label="Job order details">
        <span className="eyebrow">Details</span>
        <p className="body jo-detail-empty">{GONE_MESSAGE}</p>
      </aside>
    );
  }

  // Who may hand this on: the agency owner, and whoever is holding it. A
  // colleague it was merely shared with sees the owner's name and no control —
  // an offer that always fails is worse than no offer.
  const signedIn = auth.status === "signed-in" ? auth.me?.user : null;
  const canAssign =
    signedIn != null && (signedIn.role === "owner" || signedIn.id === row.assigned_user_id);

  return (
    <aside className="card jo-detail" aria-label="Job order details">
      <div className="jo-detail-head">
        <span className="eyebrow">Details</span>
        <ReviewBadge status={row.review_status} />
        {/* Beside the review state rather than buried below the fields: it is
            a reason to read this row, so it has to be visible before anyone
            decides whether to. */}
        {flagged(row) && <ProtectedBadge />}
      </div>

      <h3 className="jo-detail-title">
        <Value text={row.job_title_raw} />
      </h3>
      <p className="jo-detail-company">
        <Value text={row.company_name_raw} />
      </p>

      {/* Above the fields, not below them: whose job order this is changes
          whether the rest is yours to act on, so it has to be read first.
          "Unassigned" is a state a recruiter can do something about, which is
          why it is said in words rather than left as an absent line. */}
      <p className="jo-detail-owner" data-testid="jo-detail-owner">
        <span className="row-k">Owner</span>
        {row.assigned_user_id && row.assignee_name ? (
          <>
            <Initials name={row.assignee_name} seed={row.assigned_user_id} size={22} />
            <span>{row.assignee_name}</span>
          </>
        ) : (
          <span className="muted">Unassigned</span>
        )}
        {row.shared_with_me && <span className="jo-detail-shared">Shared with you</span>}
      </p>

      <QualityNote row={row} />

      <div className="rows jo-detail-rows">
        <Row k="Received" v={day(row.received_datetime)} />
        <Row k="Location" v={row.location_raw} />
        <Row k="Hours" v={row.working_hours_raw} />
        <Row k="Duration" v={row.duration_raw} />
        <div className="row">
          <span className="row-k">Salary</span>
          <span>
            <Salary row={row} />
          </span>
        </div>
      </div>

      <DecodedCodes row={row} />

      <Prose k="Requirements" text={row.requirements} />
      <Prose k="Description" text={row.job_description} />

      <PlacementForm row={placement} onSaved={setPlacement} />

      {/* Carries this job order to the candidates page rather than growing a
          job-order picker over there — the recruiter already knows which
          role they are filling. A plain link, not a fetch: the candidates
          page reads `eligible_for` from the URL itself and does its own 409
          handling if the placement type turns out not to be set. */}
      <p className="jo-detail-find">
        <a
          className="btn btn-secondary"
          href={`/dashboard/candidates?eligible_for=${encodeURIComponent(placement.id)}`}
        >
          Find candidates for this role
        </a>
      </p>

      {/* Provenance, not a link. We hold an id for the message, not a URL we
          can promise still resolves in the user's Outlook — offering one that
          404s is worse than offering none. The id is here so a disputed row
          can be traced back to the email it came from. */}
      {/* Below the job order itself, above the provenance: the shortlist is
          an answer to what the email asked for, so it only makes sense once
          the requirements above have been read. Keyed by the row along with
          the rest of the panel, so moving the selection starts it over rather
          than leaving one job order's shortlist under another's title. */}
      {/* Keyed on the placement fields too, on top of the row id `Detail` is
          already keyed by: saving a new placement type changes which MOM
          rules every candidate below is checked against, and the eligibility
          fetch inside `Shortlist` caches by candidate id with no way to know
          the job order under it just changed. Remounting is what forces it
          to ask again rather than show yesterday's answer next to today's
          placement type. */}
      <Shortlist
        key={`${placement.placement_type ?? ""}-${placement.sex_requirement ?? ""}`}
        row={row}
      />

      <div className="jo-detail-source">
        <span className="row-k">Source</span>
        <p className="body jo-sub">
          Read from one email in the connected mailbox. Every value above is the sender&rsquo;s,
          not ours.
        </p>
        <div className="rows">
          <Row k="Message id" v={row.internet_message_id} empty="Not recorded" />
          <Row k="Graph id" v={row.graph_message_id} empty="Not recorded" />
        </div>
      </div>

      {/* Claim sits here, after the job order has been read, and deliberately
          not on a list row: taking one is taking responsibility for it, and
          that decision should follow the requirements above rather than a
          scan of the table at 9pm. */}
      {(row.assigned_user_id === null || canAssign) && (
        <div className="jo-detail-own">
          {row.assigned_user_id === null && (
            <>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void move(() => onClaim(row.id))}
                disabled={moving}
              >
                {moving ? "Claiming…" : "Claim this job order"}
              </button>
              <p className="body jo-sub jo-detail-hint">
                Nobody is working this one yet. Claiming it puts your name on it and makes it
                yours to edit.
              </p>
            </>
          )}

          {canAssign && (
            <MemberSelect
              value={row.assigned_user_id}
              onChange={(userId) => void move(() => onAssign(row.id, userId))}
              allowNone
              label="Assign to"
            />
          )}

          {notice && (
            <p className="body jo-detail-error" role="alert">
              {notice}
            </p>
          )}
        </div>
      )}

      <div className="jo-detail-actions">
        <button
          type="button"
          className={reviewed ? "btn btn-secondary" : "btn btn-primary"}
          onClick={toggle}
          disabled={saving}
          aria-pressed={reviewed}
        >
          {saving ? "Saving…" : reviewed ? "Mark as not reviewed" : "Mark as reviewed"}
        </button>
        <p className="body jo-sub jo-detail-hint">
          {reviewed
            ? "Someone has checked this row against the email."
            : "Marking it reviewed records that a person has read this against the email. It changes nothing about the extraction."}
        </p>
      </div>

      {/* A failure has to say so. Silently leaving the badge unchanged would
          let someone believe they had signed off a row they had not. */}
      {error && (
        <p className="body jo-detail-error" role="alert">
          {error}
        </p>
      )}
    </aside>
  );
}

function Row({ k, v, empty }: { k: string; v: string | null; empty?: string }) {
  return (
    <div className="row">
      <span className="row-k">{k}</span>
      <span className={v ? undefined : "muted"}>
        {v ? <Breakable text={v} /> : (empty ?? "Not mentioned")}
      </span>
    </div>
  );
}

function Prose({ k, text }: { k: string; text: string | null }) {
  return (
    <div className="jo-detail-prose">
      <span className="row-k">{k}</span>
      <p className={text ? "body" : "body muted"}>
        {text ? <Breakable text={text} /> : "Not mentioned"}
      </p>
    </div>
  );
}
