"use client";

/**
 * The prominent "analysis is running" indicator for the Job and Candidate
 * Intelligence panels.
 *
 * The analysis moved to DeepSeek, which is slower than the Cerebras path it
 * replaced, so the waiting state has to say "something is happening" loudly
 * rather than with a quiet dot. This is that: a spinning ring next to a
 * bold title with three bouncing dots, an indeterminate progress bar, an
 * honest note that the three stages run in sequence, and shimmering skeleton
 * lines where the result fields will land. The skeletons mimic the actual
 * result layout (fields and lists), so the reader is watching the answer
 * take shape rather than staring at a blank panel.
 *
 * It is deliberately not tied to real per-stage progress: the API only says
 * pending/running/done, so the animation communicates motion and duration,
 * not a fabricated percentage.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export function AnalysisProgress({
  subject,
  verb = "Analysing",
  note = "The three stages run in sequence — this can take a minute or two.",
}: {
  subject: string;
  /** The action word before the subject — "Analysing" for intelligence, "Searching" for external sources. */
  verb?: string;
  /** The honest duration note under the bar; defaults to the intelligence stages copy. */
  note?: string;
}) {
  return (
    <div className="intel-progress" role="status">
      <div className="intel-progress-head">
        <span className="intel-spinner" aria-hidden="true" />
        <p className="intel-progress-title">
          {verb} {subject}
          <span className="intel-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        </p>
      </div>
      <div className="intel-bar" aria-hidden="true">
        <span className="intel-bar-track" />
      </div>
      <p className="intel-progress-note">{note}</p>
      <div className="intel-skeleton" aria-hidden="true">
        <span className="intel-skel intel-skel-field" />
        <span className="intel-skel intel-skel-field" />
        <span className="intel-skel intel-skel-list" />
        <span className="intel-skel intel-skel-list" />
        <span className="intel-skel intel-skel-field" />
      </div>
    </div>
  );
}
