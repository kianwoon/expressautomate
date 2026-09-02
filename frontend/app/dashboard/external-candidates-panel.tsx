"use client";

import {
  type ExternalCandidate,
  type ExternalSearchResults,
  type ExternalTaskStatus,
  platformLabel,
} from "./external-candidates";

/**
 * The External Candidates panel — presentational only.
 *
 * The search state (start button, polling, results) lives in the
 * `useExternalCandidates` hook, called once from `Detail`. This component
 * renders what the hook produced, in the same visual grammar as the three
 * Job Intelligence stage panels (`Stage`/`Field`/`List` idioms), so a fifth
 * tab in the same modal reads as a sibling rather than an embed.
 *
 * The panel shows what the career bot spec (§4) says a recruiter needs to
 * judge a stranger: who they are, why they matched (score + reason), which
 * platform they came from (a chip keyed off `source`), what is missing
 * (gaps), and how well the profile holds up (credibility). A source link
 * accompanies every row that has a URL — the name is itself a link, and the
 * "Open profile" chip in the meta row makes the way out explicit even when
 * the scan pattern skips the name — the spec's "every result is traceable"
 * rule, rendered.
 *
 * allow-hardcode: user-facing copy rendered to the page, not a list anything
 * is matched against.
 */

export type ExternalPanelState = {
  /** Whether an analysis with a search plan exists — without one the button
   *  is refused server-side with a 409, so it is offered disabled here. */
  hasSearchPlan: boolean;
};

const NOTHING_YET =
  'No search plan yet. Use "Run analysis" at the top — external search uses the plan from the Search tab.';

const HUMAN_NEEDED =
  "The search needs a human on the external service (a login or verification step). It is paused — try again later or contact support.";

export function ExternalCandidatesStage({
  state,
  starting,
  startError,
  taskStatus,
  taskError,
  results,
  resultsError,
  onFind,
}: {
  state: ExternalPanelState;
  starting: boolean;
  startError: string | null;
  taskStatus: ExternalTaskStatus | null;
  taskError: string | null;
  results: ExternalSearchResults | null;
  resultsError: string | null;
  onFind: () => void;
}) {
  return (
    <div className="jo-intel-stage" data-testid="jo-external-panel">
      <div className="jo-external-head">
        <h4 className="jo-intel-stage-title">External candidates</h4>
        <button
          type="button"
          className="jo-external-find"
          onClick={onFind}
          disabled={starting || !state.hasSearchPlan || isWorking(taskStatus)}
        >
          {buttonLabel(starting, taskStatus)}
        </button>
      </div>
      {!state.hasSearchPlan && <p className="body src-note">{NOTHING_YET}</p>}
      {startError && (
        <p className="body src-error" role="alert">
          {startError}
        </p>
      )}
      {taskError && (
        <p className="body src-error" role="alert">
          {taskError}
        </p>
      )}
      {taskStatus === "paused" && (
        <p className="body src-note" role="alert">
          {HUMAN_NEEDED}
        </p>
      )}
      {isWorking(taskStatus) && (
        <p className="body src-note">Searching external sources — this takes a minute or two.</p>
      )}
      {resultsError && (
        <p className="body src-error" role="alert">
          {resultsError}
        </p>
      )}
      {results && <Results results={results} />}
    </div>
  );
}

function isWorking(status: ExternalTaskStatus | null): boolean {
  return status === "pending" || status === "running" || status === "waiting_approval";
}

function buttonLabel(starting: boolean, status: ExternalTaskStatus | null): string {
  if (starting) return "Starting…";
  if (isWorking(status)) return "Searching…";
  return "Find External Candidates";
}

function Results({ results }: { results: ExternalSearchResults }) {
  if (results.results.length === 0) {
    return (
      <p className="body src-note">
        {results.summary ?? "No external candidates matched this search."}
      </p>
    );
  }
  return (
    <>
      {results.summary && <p className="body jo-sub">{results.summary}</p>}
      <ul className="jo-external-list">
        {results.results.map((candidate) => (
          <ExternalRow key={candidate.id} candidate={candidate} />
        ))}
      </ul>
    </>
  );
}

function ExternalRow({ candidate }: { candidate: ExternalCandidate }) {
  const score = Math.round(candidate.match_score);
  const platform = platformLabel(candidate);
  const sourceUrl =
    typeof candidate.source_url === "string" && candidate.source_url.trim()
      ? candidate.source_url.trim()
      : null;
  return (
    <li className="jo-external-row" data-testid="jo-external-row">
      <div className="jo-external-row-head">
        <span className="jo-external-row-title">
          {sourceUrl ? (
            <a
              className="jo-external-name"
              href={sourceUrl}
              target="_blank"
              rel="noreferrer noopener"
            >
              {candidate.title}
            </a>
          ) : (
            <span className="jo-external-name">{candidate.title}</span>
          )}
          {platform && (
            <span
              className="jo-external-chip jo-external-platform"
              data-testid="jo-external-platform"
              title={`Found on ${platform}`}
            >
              {platform}
            </span>
          )}
        </span>
        <span className="jo-external-score" title={`Match score ${candidate.match_score} of 100`}>
          {score}
        </span>
      </div>
      {candidate.subtitle && <p className="body jo-sub">{candidate.subtitle}</p>}
      <p className="body jo-external-reason">
        {candidate.match_reason ?? "Matched the search plan."}
      </p>
      <div className="jo-external-meta">
        {sourceUrl && (
          <a
            className="jo-external-chip jo-external-open"
            data-testid="jo-external-open"
            href={sourceUrl}
            target="_blank"
            rel="noreferrer noopener"
          >
            Open profile ↗
          </a>
        )}
        {candidate.location && <span className="jo-external-chip">{candidate.location}</span>}
        {candidate.skills &&
          candidate.skills.slice(0, 6).map((skill) => (
            <span key={skill} className="jo-external-chip">
              {skill}
            </span>
          ))}
      </div>
      {candidate.gaps && candidate.gaps.length > 0 && (
        <p className="body jo-sub jo-external-gaps">
          Missing: {candidate.gaps.map(String).join(", ")}
        </p>
      )}
      {candidate.credibility && (
        <p className="body jo-sub">
          Credibility {Math.round(candidate.credibility.score)}
          {candidate.credibility.flags.length > 0 &&
            ` — flags: ${candidate.credibility.flags.join(", ")}`}
        </p>
      )}
      {candidate.recommended_action && (
        <p className="body jo-sub">{candidate.recommended_action}</p>
      )}
    </li>
  );
}
