"use client";

import { useState } from "react";

import { Breakable } from "../breakable";
import { Salary, Value, day } from "./format";
import type { Opportunity } from "./opportunities";
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

export function DetailPanel({
  row,
  onReview,
}: {
  row: Opportunity | null;
  onReview: (id: string, reviewed: boolean) => Promise<string | null>;
}) {
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
  return <Detail key={row.id} row={row} onReview={onReview} />;
}

function Detail({
  row,
  onReview,
}: {
  row: Opportunity;
  onReview: (id: string, reviewed: boolean) => Promise<string | null>;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reviewed = row.review_status === "reviewed";

  async function toggle() {
    if (saving) return;
    setSaving(true);
    setError(null);
    setError(await onReview(row.id, !reviewed));
    setSaving(false);
  }

  return (
    <aside className="card jo-detail" aria-label="Job order details">
      <div className="jo-detail-head">
        <span className="eyebrow">Details</span>
        <ReviewBadge status={row.review_status} />
      </div>

      <h3 className="jo-detail-title">
        <Value text={row.job_title_raw} />
      </h3>
      <p className="jo-detail-company">
        <Value text={row.company_name_raw} />
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

      <Prose k="Requirements" text={row.requirements} />
      <Prose k="Description" text={row.job_description} />

      {/* Provenance, not a link. We hold an id for the message, not a URL we
          can promise still resolves in the user's Outlook — offering one that
          404s is worse than offering none. The id is here so a disputed row
          can be traced back to the email it came from. */}
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
