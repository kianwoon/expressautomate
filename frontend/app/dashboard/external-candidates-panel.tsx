"use client";

import { useState } from "react";

import {
  type ExternalCandidate,
  type ExternalSearchResults,
  type ExternalTaskStatus,
  type IdentityEvidenceItem,
  type IdentityResolutionSummary,
  type IdentityResolveMode,
  type ResolvedIdentity,
  platformLabel,
  summaryLine,
} from "./external-candidates";
import { AnalysisProgress } from "./intel-progress";

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
  identities,
  identityError,
  resolvingFor,
  identityHistory,
  onResolveIdentity,
  onReopenIdentity,
}: {
  state: ExternalPanelState;
  starting: boolean;
  startError: string | null;
  taskStatus: ExternalTaskStatus | null;
  taskError: string | null;
  results: ExternalSearchResults | null;
  resultsError: string | null;
  onFind: () => void;
  /** Per-candidate resolved identities, keyed by candidate id (§24). */
  identities?: Record<string, ResolvedIdentity>;
  identityError?: string | null;
  /** The candidate id currently being resolved, or null (§29 "Resolving"). */
  resolvingFor?: string | null;
  /** The job order's resolution history, newest first — the past-results
   *  list the modal offers (§28). */
  identityHistory?: IdentityResolutionSummary[];
  onResolveIdentity?: (candidate: ExternalCandidate, mode?: IdentityResolveMode) => void;
  onReopenIdentity?: (resolutionId: string) => void;
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
        <AnalysisProgress
          subject="external sources"
          verb="Searching"
          note="Searching external sources — this takes a minute or two."
        />
      )}
      {resultsError && (
        <p className="body src-error" role="alert">
          {resultsError}
        </p>
      )}
      {identityError && (
        <p className="body src-error" role="alert">
          {identityError}
        </p>
      )}
      {results && (
        <Results
          results={results}
          identities={identities}
          resolvingFor={resolvingFor}
          identityHistory={identityHistory}
          onResolveIdentity={onResolveIdentity}
          onReopenIdentity={onReopenIdentity}
        />
      )}
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

function Results({
  results,
  identities,
  resolvingFor,
  identityHistory,
  onResolveIdentity,
  onReopenIdentity,
}: {
  results: ExternalSearchResults;
  identities?: Record<string, ResolvedIdentity>;
  resolvingFor?: string | null;
  identityHistory?: IdentityResolutionSummary[];
  onResolveIdentity?: (candidate: ExternalCandidate, mode?: IdentityResolveMode) => void;
  onReopenIdentity?: (resolutionId: string) => void;
}) {
  const line = summaryLine(results.summary);
  if (results.results.length === 0) {
    return (
      <p className="body src-note">
        {line ?? "No external candidates matched this search."}
      </p>
    );
  }
  return (
    <>
      {line && <p className="body jo-sub">{line}</p>}
      <ul className="jo-external-list">
        {results.results.map((candidate) => (
          <ExternalRow
            key={candidate.id}
            candidate={candidate}
            identity={identities?.[candidate.id]}
            resolving={resolvingFor === candidate.id}
            history={identityHistory}
            onResolveIdentity={onResolveIdentity}
            onReopenIdentity={onReopenIdentity}
          />
        ))}
      </ul>
    </>
  );
}

/** §16/§28 labels for a resolution status. */
function statusLabel(status: ResolvedIdentity["status"]): string {
  if (status === "resolved") return "Resolved";
  if (status === "probable") return "Probable";
  if (status === "needs_context") return "Needs more detail";
  return "Not resolved";
}

function ExternalRow({
  candidate,
  identity,
  resolving,
  history,
  onResolveIdentity,
  onReopenIdentity,
}: {
  candidate: ExternalCandidate;
  identity?: ResolvedIdentity;
  resolving?: boolean;
  history?: IdentityResolutionSummary[];
  onResolveIdentity?: (candidate: ExternalCandidate, mode?: IdentityResolveMode) => void;
  onReopenIdentity?: (resolutionId: string) => void;
}) {
  const score = Math.round(candidate.match_score);
  const platform = platformLabel(candidate);
  const [showModal, setShowModal] = useState(false);
  const sourceUrl =
    typeof candidate.source_url === "string" && candidate.source_url.trim()
      ? candidate.source_url.trim()
      : null;
  // Past results for this candidate, newest first — what the modal's
  // "Past results" list reopens (§28).
  const past = (history ?? []).filter((h) => h.candidate_key === candidate.id);
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
          <span
            className="jo-external-chip jo-external-identity"
            data-testid="jo-identity-badge"
            data-status={identity?.status ?? "none"}
          >
            {identity
              ? `Identity: ${statusLabel(identity.status)}`
              : "Identity: Not resolved"}
          </span>
          {identity?.cached && (
            <span
              className="jo-external-chip jo-identity-cached"
              data-testid="jo-identity-cached"
              title="Served from cache — use Refresh for a fresh web search"
            >
              Cached
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
        {identity ? (
          <>
            <button
              type="button"
              className="jo-external-chip jo-identity-view"
              data-testid="jo-identity-view"
              onClick={() => setShowModal(true)}
            >
              View
            </button>
            {onResolveIdentity && (
              <>
                <button
                  type="button"
                  className="jo-external-chip jo-identity-refresh"
                  data-testid="jo-identity-refresh"
                  title="Re-search with fresh web results (bypasses cache)"
                  aria-label="Re-search with fresh web results (bypasses cache)"
                  onClick={() => onResolveIdentity(candidate, "refresh")}
                  disabled={resolving}
                >
                  {resolving ? "Searching…" : "Refresh"}
                </button>
                <button
                  type="button"
                  className="jo-external-chip jo-identity-deep"
                  data-testid="jo-identity-deep"
                  title="Deep search — more queries, fresh web results (bypasses cache)"
                  aria-label="Deep search — more queries with fresh web results (bypasses cache)"
                  onClick={() => onResolveIdentity(candidate, "deep")}
                  disabled={resolving}
                >
                  {resolving ? "Searching…" : "Deep"}
                </button>
              </>
            )}
          </>
        ) : (
          onResolveIdentity && (
            <button
              type="button"
              className="jo-external-chip jo-identity-resolve"
              data-testid="jo-identity-resolve"
              onClick={() => onResolveIdentity(candidate)}
              disabled={resolving}
            >
              {resolving ? "Searching…" : "Resolve Identity"}
            </button>
          )
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
      {showModal && identity && (
        <IdentityModal
          identity={identity}
          past={past}
          onClose={() => setShowModal(false)}
          onReopen={onReopenIdentity}
        />
      )}
    </li>
  );
}

/** One display group of matched evidence: the backend emits one item per
 *  source URL (§13), so the same signal repeats across pages. Grouping is a
 *  display concern only — contradiction and same-name rows stay per-item. */
type EvidenceGroup = {
  type: string;
  value: string;
  domains: string[];
  count: number;
};

/** Group by (type, value), preserving first-seen order. */
function groupEvidence(items: IdentityEvidenceItem[]): EvidenceGroup[] {
  const map = new Map<string, EvidenceGroup>();
  for (const item of items) {
    const key = `${item.type}\u0000${item.value}`;
    let group = map.get(key);
    if (!group) {
      group = { type: item.type, value: item.value, domains: [], count: 0 };
      map.set(key, group);
    }
    group.count += 1;
    if (item.source_domain && !group.domains.includes(item.source_domain)) {
      group.domains.push(item.source_domain);
    }
  }
  return [...map.values()];
}

/** The " — {source}" note: one page names its domain, several name the domain
 *  with a count; differing domains list up to two. */
function groupNote(group: EvidenceGroup): string | null {
  if (group.domains.length === 0) return null;
  if (group.count <= 1) return group.domains[0];
  if (group.domains.length === 1) return `${group.domains[0]} ×${group.count}`;
  const shown = group.domains.slice(0, 2).join(", ");
  const extra = group.domains.length > 2 ? ` +${group.domains.length - 2}` : "";
  return `${shown}${extra} ×${group.count}`;
}

/** §28 — the resolution modal: confidence, the matched-evidence checklist,
 *  the professional profile link, a freshness warning when the web disagrees
 *  with the record, and the candidate's past results to reopen. */
function IdentityModal({
  identity,
  past,
  onClose,
  onReopen,
}: {
  identity: ResolvedIdentity;
  past: IdentityResolutionSummary[];
  onClose: () => void;
  onReopen?: (resolutionId: string) => void;
}) {
  const profile = identity.canonical_profile_url;
  // §28: the ✓ checklist names the matched signals. Contradictions are shown
  // as ✗ rows rather than hidden, so a recruiter can see *why* an identity is
  // unresolved; zero-weight same-name profile notes are shown separately.
  const matched = identity.evidence.filter((e) => (e.weight ?? 0) > 0);
  const contradictions = identity.evidence.filter(
    (e) => e.type === "contradiction" || (e.weight ?? 0) < 0,
  );
  const sameName = identity.evidence.filter((e) => e.type === "same_name_profile");
  const matchedGroups = groupEvidence(matched);
  return (
    <div
      className="jo-identity-modal-backdrop"
      data-testid="jo-identity-modal"
      role="dialog"
      aria-modal="true"
      aria-label="Resolved identity"
    >
      <div className="jo-identity-modal">
        <div className="jo-identity-modal-head">
          <h4 className="jo-intel-stage-title">Identity confidence: {identity.confidence}%</h4>
          <button
            type="button"
            className="jo-external-chip"
            onClick={onClose}
            aria-label="Close"
          >
            Close
          </button>
        </div>
        <p className="body jo-sub">
          {statusLabel(identity.status)} · {identity.queries_used} search
          {identity.queries_used === 1 ? "" : "es"} used
          {identity.cached ? " · from cache" : ""}
        </p>
        {identity.freshness_status === "possible_change" && (
          <p className="body src-note" role="alert">
            Employment may have changed — the web now shows a different
            employer than the record.
          </p>
        )}
        {identity.evidence.some((e) => e.type === "contradiction") && (
          <p className="body src-error" role="alert">
            Conflicting evidence found — this identity is not safe to enrich
            automatically.
          </p>
        )}
        <h5 className="jo-sub">Matched evidence</h5>
        {matched.length === 0 && contradictions.length === 0 ? (
          <p className="body src-note">No corroborating evidence found.</p>
        ) : (
          <ul className="jo-identity-evidence" data-testid="jo-identity-evidence">
            {matchedGroups.map((group, index) => {
              const note = groupNote(group);
              return (
                <li
                  key={`${group.type}-${index}`}
                  className="body"
                  data-testid="jo-identity-evidence-group"
                >
                  ✓ {group.value}
                  {note && <span className="jo-sub"> — {note}</span>}
                </li>
              );
            })}
            {contradictions.map((item, index) => (
              <li
                key={`contra-${item.type}-${index}`}
                className="body src-error"
                data-testid="jo-identity-contradiction"
              >
                ✗ {item.value}
                {item.source_domain && (
                  <span className="jo-sub"> — {item.source_domain}</span>
                )}
              </li>
            ))}
          </ul>
        )}
        {sameName.length > 0 && (
          <p className="body src-note" data-testid="jo-identity-same-name">
            {sameName.length === 1
              ? "A same-name profile was found"
              : "Same-name profiles were found"}{" "}
            — none corroborates your employer, title or location.
          </p>
        )}
        {profile && (
          <p className="body">
            Professional profile found:{" "}
            <a href={profile} target="_blank" rel="noreferrer noopener">
              {profile}
            </a>
          </p>
        )}
        {past.length > 0 && (
          <>
            <h5 className="jo-sub">Past results</h5>
            <ul className="jo-identity-history" data-testid="jo-identity-history">
              {past.map((row) => (
                <li key={row.id} className="body">
                  <button
                    type="button"
                    className="jo-identity-history-open"
                    data-testid="jo-identity-history-open"
                    onClick={() => onReopen?.(row.id)}
                  >
                    {statusLabel(row.status)} · {row.confidence}%
                    {row.created_at ? ` · ${row.created_at.slice(0, 10)}` : ""}
                    {row.expired ? " · expired" : ""}
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
