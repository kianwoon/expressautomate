"use client";

import type { Opportunity, QualityState, ReviewStatus } from "./opportunities";

/**
 * How complete an extraction is — and deliberately not how sure the model was.
 *
 * The design this replaces showed a ring reading "78% confidence". That number
 * would have been the model's own self-report, and a percentage printed beside
 * a job order is read as a measurement: a recruiter would reasonably conclude
 * that roughly four rows in five like this one are right. Nothing measures
 * that. The model's confidence is stored (`model_confidence` on the extraction
 * table) because it decides which model runs next, and it is never surfaced as
 * a probability for exactly this reason.
 *
 * What is shown instead is countable and checkable by the person reading it:
 * how many of the fields we look for the email actually stated, and which of
 * three states that puts the row in. "7 of 9 values found in the email" is a
 * fact about the email. Anyone who doubts it can open the row and count.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

const LABELS: Record<QualityState, string> = {
  verified: "Verified",
  likely: "Likely",
  needs_review: "Needs review",
};

/** What each state means, in the words we would use out loud. */
const MEANINGS: Record<QualityState, string> = {
  verified: "Every value below was stated in the email itself.",
  likely: "Most values were stated in the email; a few were read from context.",
  needs_review: "Enough was missing or ambiguous that someone should read this one.",
};

export function qualityLabel(state: QualityState): string {
  return LABELS[state] ?? LABELS.needs_review;
}

/** "7 of 9 values found in the email" — the sentence, once, in one place. */
export function foundSentence(row: Opportunity): string {
  return `${row.verified_fields} of ${row.total_fields} values found in the email`;
}

/** The compact form, for a table cell. */
export function QualityBadge({ row }: { row: Opportunity }) {
  return (
    <span className="jo-quality" data-state={row.quality_state}>
      <span className="jo-quality-dot" aria-hidden="true" />
      <span>{qualityLabel(row.quality_state)}</span>
      {/* The counted part is what makes the label mean anything, so it travels
          with it rather than living only in the panel. */}
      <span className="jo-quality-count">
        {row.verified_fields}/{row.total_fields}
      </span>
    </span>
  );
}

/** The long form, for the detail panel, where there is room to say why. */
export function QualityNote({ row }: { row: Opportunity }) {
  return (
    <div className="jo-quality-note">
      <span className="jo-quality" data-state={row.quality_state}>
        <span className="jo-quality-dot" aria-hidden="true" />
        <span>
          {qualityLabel(row.quality_state)} · {foundSentence(row)}
        </span>
      </span>
      <p className="body jo-sub">{MEANINGS[row.quality_state] ?? MEANINGS.needs_review}</p>
      <p className="body jo-sub">
        This is a count of what the email said, not a score. We do not show how confident the model
        was, because that is not a measurement of whether this row is right.
      </p>
    </div>
  );
}

const REVIEW_LABELS: Record<ReviewStatus, string> = {
  ready: "New",
  needs_review: "Needs review",
  reviewed: "Reviewed",
};

export function reviewLabel(status: ReviewStatus): string {
  return REVIEW_LABELS[status] ?? REVIEW_LABELS.ready;
}

export function ReviewBadge({ status }: { status: ReviewStatus }) {
  return (
    <span className="jo-review" data-state={status}>
      {reviewLabel(status)}
    </span>
  );
}
